"""Checks that a rotation sweep runs before its first write.

The engine in `rotation.py` states two assumptions that its caller has to
validate (see its module docstring): every request is bounded, and the clocks
of the runs differ by at most a margin. This module checks them against the
real deployment. Each check raises `PreflightError`, and each message says
what to fix. Nothing here changes any state.

- OpenBao ends a request after the limit of the listener that received it.
  The listener's own `max_request_duration` applies, else the server-wide
  `default_max_request_duration`, else OpenBao's documented default of 90
  seconds. Every listener's effective limit must not exceed the engine's
  request timeout. See https://openbao.org/docs/configuration/listener/tcp/
  and https://openbao.org/docs/next/configuration/.
- A config only counts once the server runs it, so every OpenBao container
  must have started after the newest update of the config's ConfigMap.
- The Kubernetes API server ends a request after its `request-timeout`, which
  is 1 minute unless a `kube-apiserver-arg` overrides it (see
  https://kubernetes.io/docs/reference/command-line-tools-reference/kube-apiserver/).
  The k3s server config must not raise it above the engine's request timeout,
  and k3s must have started after its config files and unit files last
  changed.
- Every Kubernetes call carries a request timeout no longer than the same
  bound. `refuse_unbounded` wraps the process boundary and refuses a call that
  does not.
- The clock of this node is synchronized by chrony (the `base/chrony` role).
  The engine needs any two runners to differ by at most the margin, so each
  runner's estimated error, plus the drift that can build up during the run,
  may use at most half of it.
"""

import re

OPENBAO_DEFAULT_MAX_REQUEST_SECONDS = 90
APISERVER_DEFAULT_REQUEST_SECONDS = 60
NTP_MAX_FREQUENCY_CORRECTION = 500e-6
_LISTENER = re.compile(r'^[ \t]*listener[ \t]+"[^"]*"[ \t]*\{', re.M)
_COMMENT_LINE = re.compile(r"^[ \t]*(#|//).*$", re.M)
_LISTENER_SETTING = re.compile(r'^[ \t]*max_request_duration[ \t]*=[ \t]*"([^"]*)"', re.M)
_SERVER_SETTING = re.compile(r'^[ \t]*default_max_request_duration[ \t]*=[ \t]*"([^"]*)"', re.M)
_APISERVER_ANY_FLAG = re.compile(r"kube-apiserver-arg[= \t]")
_APISERVER_FLAG = re.compile(r"kube-apiserver-arg[= \t]+[\"']?request-timeout=([^\s\"',\]]+)")
_APISERVER_VALUE = re.compile(r"request-timeout=([^\s\"',\]]+)")
_YAML_KEY = re.compile(r"^[ \t]*kube-apiserver-arg[ \t]*:(.*)$")
_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
_TIMEOUT_FLAG = re.compile(r"--request-timeout=(\d+)s")
# `chronyc -c tracking`: reference id, name, stratum, reference time, system
# time offset, last offset, rms offset, frequency, residual frequency, skew,
# root delay, root dispersion, update interval, leap status.
_TRACKING_FIELDS = 14


class PreflightError(RuntimeError):
    """A check refused the run. The message says what to fix."""


def parse_duration_seconds(text):
    """Seconds in a Go-style duration such as `90s`, `1m30s` or `1h`. Raise
    ValueError when the text is not one."""
    text = text.strip()
    parts = list(_DURATION.finditer(text))
    if not parts or "".join(part.group(0) for part in parts) != text:
        raise ValueError("not a duration")
    return sum(float(part.group(1)) * _UNIT_SECONDS[part.group(2)] for part in parts)


def _listener_bodies(text):
    """(server_text, [listener body, ...]) of an OpenBao config. Comment lines
    are dropped, and the server text no longer holds the listener bodies."""
    text = _COMMENT_LINE.sub("", text)
    bodies, server, position = [], [], 0
    for match in _LISTENER.finditer(text):
        if match.start() < position:
            continue
        depth, index = 1, match.end()
        while index < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[index], 0)
            index += 1
        if depth:
            raise PreflightError(
                "a listener stanza in the OpenBao config is not closed. Fix the config and restart OpenBao"
            )
        server.append(text[position:match.start()])
        bodies.append(text[match.end():index - 1])
        position = index
    server.append(text[position:])
    return "".join(server), bodies


def _duration_setting(pattern, text, name):
    match = pattern.search(text)
    if match is None:
        return None
    try:
        return parse_duration_seconds(match.group(1))
    except ValueError:
        raise PreflightError(
            f"{name} in the OpenBao config is not a duration such as \"60s\". Fix the value and restart OpenBao"
        ) from None


def check_openbao_request_duration(config_text, request_timeout):
    """Refuse unless every listener's effective request limit is at most
    `request_timeout`. The effective limit is the listener's own
    `max_request_duration`, else the server-wide `default_max_request_duration`,
    else OpenBao's default. Return the largest effective limit."""
    fix = (
        f"Set openbao_max_request_duration_seconds to {request_timeout} or less, run the openbao role, "
        "and restart the OpenBao pods"
    )
    server_text, bodies = _listener_bodies(config_text)
    if not bodies:
        raise PreflightError(f"the OpenBao config has no listener stanza, so no request limit can be established. {fix}")
    default = _duration_setting(_SERVER_SETTING, server_text, "default_max_request_duration")
    if default is None:
        default = OPENBAO_DEFAULT_MAX_REQUEST_SECONDS
    effective = []
    for number, body in enumerate(bodies, start=1):
        own = _duration_setting(_LISTENER_SETTING, body, "max_request_duration")
        limit = default if own is None else own
        effective.append(limit)
        if limit > request_timeout:
            where = "its own max_request_duration" if own is not None else "the server default"
            raise PreflightError(
                f"listener {number} of the OpenBao config allows a request {limit:g} seconds (from {where}), "
                f"which is longer than the request timeout of {request_timeout} seconds. {fix}"
            )
    return max(effective)


def check_running_config(config_times, pods, config_name):
    """Refuse unless every container of every OpenBao pod started after the
    newest update of the config's ConfigMap. `config_times` are the Unix times
    of its managed-field updates. `pods` is a list of dicts with `name` and
    `containers`, each container holding `name` and `started_at` (None when it
    is not running)."""
    fix = (
        "Restart the OpenBao pods so that they load the current config (delete them and let the StatefulSet "
        "recreate them), unseal OpenBao, and run the rotation again"
    )
    if not config_times:
        raise PreflightError(
            f"the ConfigMap {config_name} records no update time, so the check cannot tell whether the "
            f"running OpenBao loaded its current content. {fix}"
        )
    if not pods:
        raise PreflightError("no OpenBao pod was found. Check that OpenBao runs and that the pod selector is right")
    newest = max(config_times)
    for pod in pods:
        if not pod["containers"]:
            raise PreflightError(f"the OpenBao pod {pod['name']} reports no running container. {fix}")
        for container in pod["containers"]:
            started = container["started_at"]
            if started is None or started <= newest:
                raise PreflightError(
                    f"container {container['name']} of the OpenBao pod {pod['name']} did not start after the "
                    f"newest update of the ConfigMap {config_name}, so it may run an older config. {fix}"
                )


def _flag_timeout(texts):
    """(any kube-apiserver-arg flag present, last request-timeout flag value)."""
    present, found = False, None
    for text in texts:
        present = present or bool(_APISERVER_ANY_FLAG.search(text))
        for match in _APISERVER_FLAG.finditer(text):
            found = match.group(1)
    return present, found


def _file_timeout(texts):
    """The last request-timeout in a `kube-apiserver-arg` key of config files."""
    found = None
    for text in texts:
        lines = text.split("\n")
        index = 0
        while index < len(lines):
            key = _YAML_KEY.match(lines[index])
            index += 1
            if key is None:
                continue
            block = [key.group(1)]
            while index < len(lines) and (lines[index].startswith((" ", "\t", "-")) or not lines[index].strip()):
                block.append(lines[index])
                index += 1
            for match in _APISERVER_VALUE.finditer("\n".join(block)):
                found = match.group(1)
    return found


def apiserver_request_timeout(config_texts, unit_texts):
    """The `request-timeout` that the k3s server gives the API server, in
    seconds, or the documented default when nothing sets one.

    `config_texts` are the config files (`config.yaml`, then `config.yaml.d`)
    and `unit_texts` are the systemd unit and its drop-ins, whose
    `--kube-apiserver-arg` flags form k3s's command line. Command line flags
    win over the config files, and a repeatable flag such as
    `kube-apiserver-arg` replaces the whole list from the files, as the k3s
    configuration page says (https://docs.k3s.io/installation/configuration).
    So when the command line carries any `kube-apiserver-arg`, the files'
    entries do not apply at all."""
    present, flag = _flag_timeout(unit_texts)
    found = flag if present else _file_timeout(config_texts)
    if found is None:
        return float(APISERVER_DEFAULT_REQUEST_SECONDS)
    try:
        return parse_duration_seconds(found)
    except ValueError:
        raise PreflightError(
            f"the k3s server config sets the API server request-timeout to \"{found}\", which is not a duration "
            "such as \"60s\". Fix the value"
        ) from None


def check_apiserver_timeout(config_texts, unit_texts, request_timeout):
    """Refuse when the API server's request limit exceeds `request_timeout`."""
    limit = apiserver_request_timeout(config_texts, unit_texts)
    if limit > request_timeout:
        raise PreflightError(
            f"the k3s server config lets the API server run a request {limit:g} seconds, which is longer than the "
            f"request timeout of {request_timeout} seconds. Remove the request-timeout override from "
            "kube-apiserver-arg or lower it, and restart k3s"
        )
    return limit


def check_k3s_files_current(files, started_at):
    """Refuse unless k3s started after every config and unit file last changed,
    so the files describe the running server. `files` is a list of
    (path, modification time) and `started_at` the Unix time k3s last
    started, or None when it is not running."""
    fix = "Restart k3s so that it loads its current config, then run the rotation again"
    if started_at is None:
        raise PreflightError(f"k3s is not running, so its start time is unknown. {fix}")
    for path, changed in files:
        if changed > started_at:
            raise PreflightError(f"{path} changed after k3s last started, so the server may run an older config. {fix}")


def check_client_timeouts(clients, request_timeout):
    """Refuse unless every client declares a request timeout within the bound."""
    for name, client in clients.items():
        declared = getattr(client, "request_timeout", None)
        if isinstance(declared, bool) or not isinstance(declared, int) or declared <= 0:
            raise PreflightError(f"the {name} client declares no request timeout. Give it one of at most {request_timeout} seconds")
        if declared > request_timeout:
            raise PreflightError(
                f"the {name} client allows {declared} seconds per call, which is longer than the request timeout "
                f"of {request_timeout} seconds. Lower the client's request timeout"
            )


def refuse_unbounded(runner, request_timeout):
    """Wrap a process runner so that it refuses, before running anything, a
    call whose process timeout is missing or longer than `request_timeout`, and
    a `kubectl` call without `--request-timeout` within the same bound."""

    def checked(argv, stdin_text="", timeout=None):
        if timeout is None or timeout > request_timeout:
            raise PreflightError(f"a call has no process timeout within {request_timeout} seconds")
        if argv and argv[0] == "kubectl":
            flags = [int(found) for arg in argv for found in _TIMEOUT_FLAG.findall(arg)]
            if len(flags) != 1 or flags[0] > request_timeout:
                raise PreflightError(
                    f"a kubectl call lacks --request-timeout of at most {request_timeout} seconds"
                )
        return runner(argv, stdin_text, timeout)

    return checked


def check_clock(tracking_csv, margin, run_seconds):
    """Refuse unless chrony reports a synchronized clock whose estimated error
    (offset plus root dispersion plus half the root delay), plus a drift
    allowance, is at most half of `margin` seconds.

    The engine needs any two runners to differ by at most `margin`, and two
    clocks that each stay within half of it differ by at most the whole. The
    check reads the clock once, so it reserves the drift that can build up
    before the engine's last timing decision: `run_seconds` times 500 parts
    per million, the largest frequency correction that NTP's clock discipline
    applies (the constant MAXFREQ in RFC 5905, appendix A.5.5.6). The caller
    passes the Lease duration as `run_seconds`: every renewal reads the clock
    again, so drift only builds up within one Lease period.
    `tracking_csv` is the output of `chronyc -c tracking`."""
    fix = "Check the base/chrony role and run `chronyc tracking` on the node"
    fields = tracking_csv.strip().split(",")
    if len(fields) != _TRACKING_FIELDS:
        raise PreflightError(f"the chrony tracking output is not readable. {fix}")
    try:
        stratum = int(fields[2])
        offset, delay, dispersion = abs(float(fields[4])), float(fields[10]), float(fields[11])
    except ValueError:
        raise PreflightError(f"the chrony tracking output is not readable. {fix}") from None
    if fields[0].strip("0") == "" or stratum == 0 or fields[13].strip() != "Normal":
        raise PreflightError(f"the node clock is not synchronized by chrony. {fix}")
    error = offset + dispersion + delay / 2
    drift = run_seconds * NTP_MAX_FREQUENCY_CORRECTION
    if error + drift > margin / 2:
        raise PreflightError(
            f"the node clock may be off by {error:.3f} seconds and can drift {drift:.3f} seconds more during a run "
            f"of {run_seconds} seconds, which together are more than half of the clock skew margin of {margin} "
            f"seconds. Each runner may use half the margin, so that any two runners differ by at most the margin. "
            f"{fix} until the offset and dispersion shrink"
        )
    return error + drift
