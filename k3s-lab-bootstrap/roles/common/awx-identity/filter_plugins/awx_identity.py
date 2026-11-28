"""Filters that turn the two AWX identity inputs into every other value.

The AWX operator names the web Service `<instance name>-service` and serves it on
port 80. The seed, the alert rules, the dashboard and the deployer call these
filters with `awx_instance_name` and `awx_namespace`, so they cannot disagree.
A filter takes its values from its arguments only: extra vars cannot reach it.
Each filter checks the shape of its inputs, because the values end up inside
PromQL strings."""
import re

from ansible.errors import AnsibleFilterError

_LABEL = r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?"
# \Z anchors the end of the string; $ would also accept a trailing newline.
_NAMESPACE = re.compile(rf"{_LABEL}\Z")
# The instance name prefixes the names of a Service, Deployments and a Secret,
# and a Service name must be a DNS-1035 label: it starts with a letter, with no
# dots. The longest suffix this repo writes is "-admin-password" (15
# characters), and a Kubernetes name has at most 63, so the instance name has at
# most 63 - 15 = 48.
_NAME = re.compile(r"(?=.{1,48}\Z)[a-z]([-a-z0-9]*[a-z0-9])?\Z")

SERVICE_PORT = 80
_NAME_RULE = "a DNS-1035 label of at most 48 characters"


def _checked(value, pattern, what, rule="a DNS label"):
    if not isinstance(value, str) or not pattern.match(value):
        raise AnsibleFilterError(
            f"{what} must be a plain Kubernetes name ({rule}), because the alert rules "
            f"and the dashboard embed it. Got {value!r}.")
    return value


def awx_service_name(instance_name):
    """The operator's Service for an AWX instance: `<name>-service`."""
    return f"{_checked(instance_name, _NAME, 'awx_instance_name', _NAME_RULE)}-service"


def awx_service_port(instance_name):
    """The operator's fixed Service port. The instance name is validated, not used."""
    _checked(instance_name, _NAME, "awx_instance_name", _NAME_RULE)
    return SERVICE_PORT


def awx_probe_instance(instance_name, namespace):
    """The `instance` label of the AWX probe series, as promsd composes it."""
    _checked(namespace, _NAMESPACE, "awx_namespace")
    return (f"{awx_service_name(instance_name)}.{namespace}.svc.cluster.local:"
            f"{SERVICE_PORT}")


class FilterModule:
    def filters(self):
        return {"awx_service_name": awx_service_name,
                "awx_service_port": awx_service_port,
                "awx_probe_instance": awx_probe_instance}
