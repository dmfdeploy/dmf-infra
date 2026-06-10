# common/admin-identity-resolve

Read the live admin user + password from a cluster K8s Secret, falling back to
caller-supplied values.

Complements `common/app-admin-facts` (which materialises OpenBao at bootstrap
time): this role is the runtime live-state reader for plays that need to
authenticate to an already-deployed app using whatever identity is actually in
the cluster, not what the role defaults assumed.

See ADR-0024 (two-identity admin model + live-state read for drift envs).

## Inputs

| Var | Required | Default | Purpose |
|---|---|---|---|
| `admin_resolve_app` | yes | — | Free-form app name (log message only) |
| `admin_resolve_secret_ns` | yes | — | K8s namespace containing the Secret |
| `admin_resolve_secret_name` | yes | — | K8s Secret name |
| `admin_resolve_username_key` | no | `username` | Secret data key for the username |
| `admin_resolve_password_key` | no | `password` | Secret data key for the password |
| `admin_resolve_fallback_username` | yes | — | Used when Secret is missing/incomplete |
| `admin_resolve_fallback_password` | yes | — | Used when Secret is missing/incomplete |

## Outputs

| Fact | Meaning |
|---|---|
| `_resolved_admin_user` | Username — Secret value if present, fallback otherwise |
| `_resolved_admin_password` | Password — Secret value if present, fallback otherwise |
| `_resolved_admin_source` | Diagnostic string for the resolution path taken |

## Example

```yaml
- name: Resolve AWX admin identity
  ansible.builtin.include_role:
    name: common/admin-identity-resolve
  vars:
    admin_resolve_app: awx
    admin_resolve_secret_ns: "{{ awx_namespace | default('awx') }}"
    admin_resolve_secret_name: "{{ awx_admin_secret_name | default('awx-admin-password') }}"
    admin_resolve_fallback_username: "{{ vault_bootstrap_admin_username | default('dmfadmin') }}"
    admin_resolve_fallback_password: "{{ vault_bootstrap_admin_password | default('') }}"

- name: Authenticate to AWX
  ansible.builtin.uri:
    url: "{{ awx_api_base }}/me/"
    url_username: "{{ _resolved_admin_user }}"
    url_password: "{{ _resolved_admin_password }}"
    force_basic_auth: true
  no_log: true
```

## Host context

Designed to run in plays targeting `k3s_control[0]`, where
`/etc/rancher/k3s/k3s.yaml` is locally readable. Matches the existing
`kubernetes.core.k8s_info` invocations in `playbooks/697-cms-awx-token.yml`
and `roles/stack/operator/awx-integration/tasks/main.yml`.
