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
_NAME = re.compile(rf"(?=.{{1,253}}\Z){_LABEL}(\.{_LABEL})*\Z")

SERVICE_PORT = 80


def _checked(value, pattern, what):
    if not isinstance(value, str) or not pattern.match(value):
        raise AnsibleFilterError(
            f"{what} must be a plain Kubernetes name, because the alert rules and the "
            f"dashboard embed it. Got {value!r}.")
    return value


def awx_service_name(instance_name):
    """The operator's Service for an AWX instance: `<name>-service`."""
    return f"{_checked(instance_name, _NAME, 'awx_instance_name')}-service"


def awx_service_port(instance_name):
    """The operator's fixed Service port. The instance name is validated, not used."""
    _checked(instance_name, _NAME, "awx_instance_name")
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
