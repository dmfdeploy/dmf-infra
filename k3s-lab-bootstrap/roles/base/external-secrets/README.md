# external-secrets

Installs External Secrets Operator (chart `2.3.0`, app `v2.3.0`) and wires a
`ClusterSecretStore` at an OpenBao/Vault endpoint. The store logs in with
AppRole (the default) or with Kubernetes auth.

## Parametric

Topology-agnostic — the same role works against in-cluster OpenBao or any
external endpoint:

- `eso_openbao_url` — `https://openbao.openbao.svc.cluster.local:8200`
  (in-cluster default) or any external HTTPS endpoint
- `eso_openbao_auth_method` — `approle` (default) or `kubernetes`. The openbao
  role reads the same variable, so set it once in the site inventory.
  `kubernetes` also needs `openbao_kubernetes_reviewer_enabled: true`, which
  stays on when you roll back.
- `eso_openbao_role_id` / `eso_openbao_secret_id` — injected by the caller for
  `approle`; never persisted to disk on cluster nodes. With `kubernetes` they
  are not needed, and they stay available for a rollback.
- `eso_openbao_ca_bundle` — optional PEM CA bundle for HTTPS targets
- `eso_crd_api_version` — `external-secrets.io/v1` (v1beta1 is NOT served by
  chart 2.x; don't flip without verifying `kubectl api-resources`)

## Kubernetes auth

With `eso_openbao_auth_method: kubernetes`, the store logs in with a token of
ESO's own ServiceAccount (`eso_service_account_name`, default
`external-secrets`). The store asks for the audience
`eso_openbao_kubernetes_audience`, and the OpenBao role of the same name
(`eso_openbao_kubernetes_role`) binds to that account and namespace. The role
and the audience come from the openbao role's variables
(`openbao_eso_kubernetes_role_name`, `openbao_eso_kubernetes_audience`).

Two variables control the move. `openbao_kubernetes_reviewer_enabled` gives
OpenBao its own ServiceAccount and the local-file reviewer config; every
Kubernetes-auth client of the mount shares it. `eso_openbao_auth_method`
switches the main store's login. To roll back, set only
`eso_openbao_auth_method` to `approle` and rerun this role. Leave the reviewer
variable on: turning it off restarts OpenBao as the default ServiceAccount while
the mount still reads the pod's own token.

The role never keeps both methods in one store. When the existing store holds
the other method's key, the task replaces the store, and a check after the apply
fails unless the store holds exactly the selected method.

On a new install, the openbao role writes the Kubernetes auth config and the
ESO role. On an installed environment, run
`playbooks/vertical-security/106-openbao-kubernetes-auth.yml` first, then this
role. The decision is `docs/decisions/0051-eso-main-store-kubernetes-auth.md` in
the umbrella repo.

## Smoke test

When `eso_smoketest_enabled=true`, creates an `ExternalSecret` reading
`eso_smoketest_remote_key` (property `eso_smoketest_remote_property`) into
a target K8s Secret in `eso_smoketest_namespace`, then asserts the key
materialized and decodes it for the final debug report.

## Invocation

See `playbooks/vertical-orchestration/100-eso.yml`. With `approle`, the
playbook's `pre_tasks` load AppRole credentials from an operator-local JSON file
by default (`eso_openbao_breakglass_file`); override `eso_openbao_role_id` /
`eso_openbao_secret_id` to supply from any other source. With `kubernetes`, the
play reads no credentials.
