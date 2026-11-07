"""Command line entry point for playbook 694 on the receipt-driven path.

Run on the control node by the born-inventory role. It reads the opaque
request id from `DMF_SETUP_REQUEST_ID`, takes the AppRole role id from
`--role-id` or from the `role_id` key of a ConfigMap in the receipt namespace,
runs the setup-only issuance through the `kubectl` transports, and prints one
JSON object with the NetBox admin token on stdout. It prints nothing on stdout
unless the SecretID accessor was destroyed. Failures print a fixed message on
stderr and exit 1; the message says whether the residue is unconfirmed.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from approle_lifecycle import setup_only  # noqa: E402
from approle_lifecycle.transports import (  # noqa: E402
    DEFAULT_KUBECONFIG,
    KubernetesTransport,
    OpenBaoTransport,
)

REQUEST_ID_ENV = "DMF_SETUP_REQUEST_ID"
NETBOX_SECRET_PATH = "secret/data/apps/netbox/runtime"
NETBOX_SECRET_FIELD = "netbox_admin_token"
ROLE_ID_KEY = "role_id"


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-id", required=True)
    parser.add_argument("--approle-name", required=True)
    parser.add_argument("--role-id", default="")
    parser.add_argument("--role-id-configmap", required=True)
    parser.add_argument("--issuer-namespace", required=True)
    parser.add_argument("--issuer-service-account", required=True)
    parser.add_argument("--issuer-openbao-role", required=True)
    parser.add_argument("--audience", action="append", required=True)
    parser.add_argument("--openbao-namespace", required=True)
    parser.add_argument("--openbao-pod", required=True)
    parser.add_argument("--kubeconfig", default=DEFAULT_KUBECONFIG)
    parser.add_argument("--executor-as", default="")
    return parser


def _published_role_id(args, clients):
    """Read the role id before the receipt is loaded, so a missing role id
    consumes no receipt."""
    try:
        return clients.kubernetes.get_configmap_value(
            setup_only.RECEIPT_NAMESPACE, args.role_id_configmap, ROLE_ID_KEY
        )
    except Exception:
        raise setup_only.SetupOnlyError(
            "role-id",
            f"no role id is available: the {ROLE_ID_KEY} key of the ConfigMap {args.role_id_configmap} "
            f"in {setup_only.RECEIPT_NAMESPACE} is missing or unreadable. "
            "Set dmf_born_inventory_setup_role_id to override it. No receipt was loaded or claimed",
        ) from None


def run_setup(args, request_id, clients, now=lambda: int(time.time())):
    """Issue, use and revoke. Return the token JSON text after cleanup."""
    config = setup_only.SetupOnlyConfig(
        env_id=args.env_id,
        approle_name=args.approle_name,
        issuer_namespace=args.issuer_namespace,
        issuer_service_account=args.issuer_service_account,
        issuer_openbao_role=args.issuer_openbao_role,
        audiences=tuple(args.audience),
    )
    found = []
    role_id = args.role_id or _published_role_id(args, clients)

    def use(secret_id):
        bao = clients.openbao
        token = bao.login_approle(role_id, secret_id)
        try:
            found.append(bao.read_kv2_field(token, NETBOX_SECRET_PATH, NETBOX_SECRET_FIELD))
        finally:
            bao.revoke_self(token)

    setup_only.issue_use_revoke(request_id, config, clients, use, now)
    return json.dumps({NETBOX_SECRET_FIELD: found[0]})


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    args = build_parser().parse_args(argv)
    request_id = environ.get(REQUEST_ID_ENV, "")
    if not request_id:
        print(f"{REQUEST_ID_ENV} is not set", file=sys.stderr)
        return 1
    clients = setup_only.SetupOnlyClients(
        kubernetes=KubernetesTransport(args.kubeconfig, args.executor_as or None),
        openbao=OpenBaoTransport(args.openbao_namespace, args.openbao_pod, args.kubeconfig),
    )
    try:
        print(run_setup(args, request_id, clients))
    except setup_only.SetupOnlyError as error:
        print(f"setup-only issuance failed at {error.stage}: {error}", file=sys.stderr)
        return 1
    except Exception:
        print("setup-only issuance failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
