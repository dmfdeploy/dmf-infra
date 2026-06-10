# common/app-admin-facts

Shared OpenBao-backed local-admin credential resolver. Two modes:

- **`materialize`** (default) — read-or-generate-and-write. Used at install
  time to seed `secret/apps/<app>/admin` and expose the resolved facts to
  downstream tasks.
- **`live-read`** — read-only. Used at runtime/provisioning when a caller
  needs the canonical admin identity without ever writing.

## Pre-condition (both modes)

The caller MUST have already included `common/openbao-session` (with
`openbao_session_mode: operator`) earlier in the same play. This role reads
`_openbao_session_pod` and `_openbao_session_client_token` from play scope and
fails loud if either is unset. A single openbao-session establishment serves
any number of app-admin-facts invocations in the same play.

## Materialize mode

```yaml
- name: Establish OpenBao operator session
  ansible.builtin.include_role:
    name: common/openbao-session
  vars:
    openbao_session_mode: operator

- name: Materialise the foo app admin
  ansible.builtin.include_role:
    name: common/app-admin-facts
  vars:
    app_admin_app_name: foo
    app_admin_fact_prefix: foo_admin
    app_admin_secret_path: secret/apps/foo/admin
    app_admin_default_username: admin
    app_admin_expected_username: admin
```

Behaviour:

- if `secret/apps/<app>/admin` already exists, reuse it
- otherwise use caller-supplied values if provided
  (`app_admin_username_input`, `app_admin_password_input`,
  `app_admin_email_input`)
- otherwise use the role defaults and generate a password on the
  operator host
- persist the resolved triple back to OpenBao if it differs from what
  was already there

## Live-read mode

```yaml
- name: Establish OpenBao operator session
  ansible.builtin.include_role:
    name: common/openbao-session
  vars:
    openbao_session_mode: operator

- name: Resolve foo admin identity for runtime use
  ansible.builtin.include_role:
    name: common/app-admin-facts
  vars:
    app_admin_mode: live-read
    app_admin_app_name: foo
    app_admin_fact_prefix: foo_admin
    app_admin_secret_path: secret/apps/foo/admin
    app_admin_fallback_candidates:
      - username: "{{ foo_admin_username | default('') }}"
        password: "{{ foo_admin_password | default('') }}"
      - username: "{{ vault_bootstrap_admin_username | default('') }}"
        password: "{{ vault_bootstrap_admin_password | default('') }}"
```

Behaviour:

- never writes, never generates, never defaults the email
- resolution is strictly **source-atomic** — each layer must supply BOTH
  username and password, or the whole layer is rejected. Username from
  OpenBao and password from a fallback can never be combined.
- resolution order:
  1. OpenBao `<secret_path>` if both `username` and `password` fields
     are non-empty → `<prefix>_source: openbao`
  2. First entry in `app_admin_fallback_candidates` where both
     `username` and `password` are non-empty →
     `<prefix>_source: fallback-candidates`
  3. Scalar `app_admin_fallback_username` + `app_admin_fallback_password`
     if both are non-empty → `<prefix>_source: fallback-scalar`
  4. Otherwise resolved values are empty strings →
     `<prefix>_source: no-fallback`
- live-read does **not** fail loud on missing credentials. Empty
  resolution is reported via `<prefix>_source: no-fallback` so consumers
  (e.g. audit playbooks) can treat empty as a finding. Non-audit
  consumers should assert non-empty themselves before use.

### Audit usage — drift detection requires explicit nulls

```yaml
- name: Resolve foo admin identity for audit comparison
  ansible.builtin.include_role:
    name: common/app-admin-facts
  vars:
    app_admin_mode: live-read    # MANDATORY — audit never writes
    app_admin_app_name: foo
    app_admin_fact_prefix: _audit_foo
    app_admin_secret_path: secret/apps/foo/admin
    app_admin_fallback_username: ""   # intentionally empty so missing
    app_admin_fallback_password: ""   # OpenBao secret surfaces as finding
```

## Outputs

| Fact | Materialize | Live-read |
|---|---|---|
| `<prefix>_username` | yes | yes |
| `<prefix>_password` | yes | yes |
| `<prefix>_email` | yes | — (not emitted) |
| `<prefix>_secret_path` | yes | yes |
| `<prefix>_source` | `openbao` \| `fallback` | `openbao` \| `fallback-candidates` \| `fallback-scalar` \| `no-fallback` |
