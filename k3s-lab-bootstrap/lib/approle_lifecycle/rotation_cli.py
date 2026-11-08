"""Command line entry point of the rotation playbook.

Run on the control node. It checks the engine's assumptions, logs in to
OpenBao as the rotation scheduler ServiceAccount, runs the rotation engine for
one registry target or for every runtime target, and prints one line per
target. A line holds only the target name, the outcome, the transaction state,
the reason code, the transaction id and the cause. No SecretID, token or
accessor is printed, and none is passed in argv or the environment.

Exit status: 0 when no target failed, 1 when a target failed, 2 when a check
or the run context refused to start, 3 when the run itself failed.
`--preflight-only` runs the checks and stops.
"""

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from approle_lifecycle import registry, rotation  # noqa: E402
from approle_lifecycle.rotation_preflight import (  # noqa: E402
    PreflightError,
    check_apiserver_timeout,
    check_client_timeouts,
    check_k3s_files_current,
    check_clock,
    check_openbao_request_duration,
    check_running_config,
    refuse_unbounded,
)
from approle_lifecycle.rotation_transports import RotationKubernetes, RotationOpenBao, run  # noqa: E402
from approle_lifecycle.transports import DEFAULT_KUBECONFIG  # noqa: E402

EXIT_OK, EXIT_TARGET_FAILED, EXIT_REFUSED, EXIT_RUN_FAILED = 0, 1, 2, 3
OPENBAO_CONFIG_KEY = "config.hcl"
# Where k3s takes its server arguments from. The config files come first and
# the systemd unit last, because a command line flag wins over the files. A
# path can be a file or a directory of files.
K3S_CONFIG_PATHS = ("/etc/rancher/k3s/config.yaml", "/etc/rancher/k3s/config.yaml.d")
K3S_UNIT_PATHS = ("/etc/systemd/system/k3s.service", "/etc/systemd/system/k3s.service.d")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--inventory-json", default="{}")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--target")
    which.add_argument("--all", action="store_true", dest="sweep")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--role", default=rotation.SCHEDULER_ROLE)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--openbao-namespace", required=True)
    parser.add_argument("--openbao-pod", required=True)
    parser.add_argument("--openbao-config-map", required=True)
    parser.add_argument("--openbao-pod-selector", required=True)
    parser.add_argument("--k3s-config-path", action="append", default=None)
    parser.add_argument("--k3s-unit-path", action="append", default=None)
    parser.add_argument("--lease-duration", type=int, default=rotation.RotationConfig(lease_namespace="x").lease_duration)
    parser.add_argument("--service-account", required=True)
    parser.add_argument("--openbao-role", required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--lease-namespace", required=True)
    parser.add_argument("--kubeconfig", default=DEFAULT_KUBECONFIG)
    parser.add_argument("--act-as-service-account", action="store_true")
    parser.add_argument("--request-timeout", type=int, default=rotation.MAX_REQUEST_SECONDS)
    parser.add_argument("--clock-skew-margin", type=int, default=rotation.CLOCK_SKEW_MARGIN_SECONDS)
    parser.add_argument("--preflight-only", action="store_true")
    return parser


def format_result(result):
    """One report line. Every value comes from a TargetResult, which holds no
    credential."""
    return (
        f"target={result.target} outcome={result.outcome} state={result.state or '-'} "
        f"reason_code={result.reason_code or '-'} transaction_id={result.transaction_id or '-'} "
        f"cause={result.cause or '-'}"
    )


def format_report(results):
    lines = [format_result(result) for result in results]
    counts = {name: sum(1 for r in results if r.outcome == name) for name in ("rotated", "not_due", "failed")}
    lines.append(f"summary rotated={counts['rotated']} not_due={counts['not_due']} failed={counts['failed']}")
    return "\n".join(lines)


def read_tracking(runner, timeout):
    fix = "Check that chrony runs on the node (the base/chrony role)"
    try:
        rc, out, _err = runner(["chronyc", "-c", "tracking"], "", timeout)
    except (OSError, subprocess.SubprocessError):
        raise PreflightError(f"chronyc did not report the clock state. {fix}") from None
    if rc != 0:
        raise PreflightError(f"chronyc did not report the clock state. {fix}")
    return out


def load_inputs(args):
    """Return (registry, inventory). Raise PreflightError when either is unreadable."""
    try:
        registry_data = registry.load(args.registry)
        inventory = json.loads(args.inventory_json)
    except (registry.RegistryError, OSError, ValueError):
        raise PreflightError("the registry could not be read or the inventory is not JSON") from None
    return registry_data, inventory


def read_k3s_files(paths):
    """The k3s files under `paths` that exist, as (path, text, modification
    time) in order. A path can be a file or a directory of files. Raise
    PreflightError when a file cannot be read."""
    found = []
    for path in paths:
        try:
            if os.path.isdir(path):
                names = sorted(name for name in os.listdir(path) if name.endswith((".yaml", ".yml", ".conf")))
                files = [os.path.join(path, name) for name in names]
            else:
                files = [path] if os.path.exists(path) else []
            for name in files:
                with open(name, encoding="utf-8") as handle:
                    found.append((name, handle.read(), os.stat(name).st_mtime))
        except OSError:
            raise PreflightError(
                f"the k3s server config at {path} could not be read, so the API server's request timeout is "
                "unknown. Run the playbook as root on a k3s server node"
            ) from None
    return found


def read_k3s_start(runner, timeout):
    """The Unix time k3s last started, or None when it is not running."""
    fix = "Run the playbook as root on a k3s server node"
    try:
        rc, out, _err = runner(["systemctl", "show", "k3s", "-p", "ActiveEnterTimestamp", "--timestamp=unix"], "", timeout)
    except (OSError, subprocess.SubprocessError):
        raise PreflightError(f"systemctl did not report when k3s started. {fix}") from None
    if rc != 0:
        raise PreflightError(f"systemctl did not report when k3s started. {fix}")
    value = out.strip().partition("=")[2]
    if not value:
        return None
    if not (value.startswith("@") and value[1:].isdigit()):
        raise PreflightError(f"systemctl reported a start time that is not a Unix time. {fix}")
    return int(value[1:])


def preflight(args, kubernetes, openbao, runner):
    """Run every check of `rotation_preflight`. Raise PreflightError."""
    timeout = args.request_timeout
    check_client_timeouts({"kubernetes": kubernetes, "openbao": openbao}, timeout)
    try:
        config = kubernetes.get_configmap(args.openbao_namespace, args.openbao_config_map)
        config_text = config["data"][OPENBAO_CONFIG_KEY]
        pods = kubernetes.list_pods(args.openbao_namespace, args.openbao_pod_selector)
    except Exception:
        raise PreflightError(
            f"the key {OPENBAO_CONFIG_KEY} of the ConfigMap {args.openbao_config_map} in "
            f"{args.openbao_namespace}, or the OpenBao pods, could not be read. Check that the openbao role has run"
        ) from None
    check_openbao_request_duration(config_text, timeout)
    check_running_config(config["update_times"], pods, args.openbao_config_map)
    config_files = read_k3s_files(args.k3s_config_path or K3S_CONFIG_PATHS)
    unit_files = read_k3s_files(args.k3s_unit_path or K3S_UNIT_PATHS)
    if not unit_files:
        raise PreflightError(
            "no k3s unit file was found, so the API server's request timeout is unknown. "
            "Run the playbook on a k3s server node"
        )
    check_apiserver_timeout([text for _p, text, _m in config_files], [text for _p, text, _m in unit_files], timeout)
    check_k3s_files_current(
        [(path, changed) for path, _t, changed in config_files + unit_files], read_k3s_start(runner, timeout)
    )
    check_clock(read_tracking(runner, timeout), args.clock_skew_margin, args.lease_duration)


def run_rotation(args, registry_data, kubernetes, openbao, inventory, now):
    """Log in as the scheduler, run the engine, and end the session."""
    context = rotation.RunContext(
        actor=args.actor, role=args.role, request_id=args.request_id, reason=args.reason
    )
    config = rotation.RotationConfig(
        lease_namespace=args.lease_namespace,
        request_timeout=args.request_timeout,
        clock_skew_margin=args.clock_skew_margin,
        lease_duration=args.lease_duration,
        inventory=inventory,
    )
    clients = rotation.RotationClients(kubernetes=kubernetes, openbao=openbao)
    rotation.validate_context(context)  # before a token is minted or a session opened
    jwt = kubernetes.request_token(args.openbao_namespace, args.service_account, args.audience)
    openbao.login_kubernetes(args.openbao_role, jwt)
    try:
        if args.sweep:
            return rotation.rotate_all(registry_data, context, clients, config, now).results
        return (rotation.rotate_target(registry_data, args.target, context, clients, config, now),)
    finally:
        try:
            openbao.revoke_self()
        except Exception:
            print("the OpenBao token revoke failed; the token expires on its own", file=sys.stderr)


def main(argv=None, runner=run, now=lambda: int(time.time())):
    args = build_parser().parse_args(argv)
    runner = refuse_unbounded(runner, args.request_timeout)
    as_user = (
        f"system:serviceaccount:{args.openbao_namespace}:{args.service_account}"
        if args.act_as_service_account
        else None
    )
    try:
        if args.request_timeout <= 0:
            raise PreflightError("--request-timeout must be a positive number of seconds")
        kubernetes = RotationKubernetes(args.kubeconfig, as_user, runner, args.request_timeout)
        openbao = RotationOpenBao(args.openbao_namespace, args.openbao_pod, args.kubeconfig, runner, args.request_timeout)
        preflight(args, kubernetes, openbao, runner)
        if args.preflight_only:
            print("preflight ok")
            return EXIT_OK
        registry_data, inventory = load_inputs(args)
        results = run_rotation(args, registry_data, kubernetes, openbao, inventory, now)
    except PreflightError as error:
        print(f"refused: {error}", file=sys.stderr)
        return EXIT_REFUSED
    except rotation.RotationError as error:
        print(f"rotation failed: reason_code={error.reason_code} {error}", file=sys.stderr)
        return EXIT_REFUSED if error.reason_code == "invalid_state" else EXIT_RUN_FAILED
    except Exception:
        print("rotation failed", file=sys.stderr)
        return EXIT_RUN_FAILED
    print(format_report(results))
    return EXIT_TARGET_FAILED if any(r.outcome == "failed" for r in results) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
