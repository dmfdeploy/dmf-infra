# external-secrets

Installs External Secrets Operator (chart `2.3.0`, app `v2.3.0`) and wires a
`ClusterSecretStore` at an OpenBao/Vault endpoint using AppRole auth.

## Parametric

Topology-agnostic — the same role works against in-cluster OpenBao or any
external endpoint:

- `eso_openbao_url` — `https://openbao.openbao.svc.cluster.local:8200`
  (in-cluster default) or any external HTTPS endpoint
- `eso_openbao_auth_method` — `approle` (kubernetes auth is a future extension)
- `eso_openbao_role_id` / `eso_openbao_secret_id` — injected by the caller;
  never persisted to disk on cluster nodes
- `eso_openbao_ca_bundle` — optional PEM CA bundle for HTTPS targets
- `eso_crd_api_version` — `external-secrets.io/v1` (v1beta1 is NOT served by
  chart 2.x; don't flip without verifying `kubectl api-resources`)

## Smoke test

When `eso_smoketest_enabled=true`, creates an `ExternalSecret` reading
`eso_smoketest_remote_key` (property `eso_smoketest_remote_property`) into
a target K8s Secret in `eso_smoketest_namespace`, then asserts the key
materialized and decodes it for the final debug report.

## Invocation

See `playbooks/24-external-secrets-operator.yml`. The playbook's `pre_tasks`
load AppRole credentials from an operator-local JSON file by default
(`eso_openbao_breakglass_file`); override `eso_openbao_role_id` /
`eso_openbao_secret_id` to supply from any other source.
