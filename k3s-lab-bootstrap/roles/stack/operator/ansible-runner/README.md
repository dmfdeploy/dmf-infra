# ansible-runner

In-cluster ansible execution foundation for configure-stage playbooks.

See ADR-0023 §Future direction. Approved spike plan: 2026-05-14.

## What this role does

Idempotent install of:

- Namespace `{{ ansible_runner_namespace }}` (default: `dmf-bootstrap`)
- ServiceAccount `{{ ansible_runner_service_account }}` (default: `ansible-runner`)
- ClusterRoleBinding granting that SA `{{ ansible_runner_cluster_role }}`
  (spike default: `cluster-admin`; narrow post-spike)

It does NOT create runner Jobs. Those are spawned per-invocation by
`dmf-env/bin/run-playbook-in-cluster.sh`, which renders
`templates/runner-job.yaml.j2` (added in Phase 2 of the spike).

## Spike scope (2026-05-14)

This role lands in Phase 1 of the runner-pod spike. Phase 2 follows:
operator wrapper + Job template + openbao-session mounted-secret mode +
end-to-end test against playbook 698.

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `ansible_runner_namespace` | `dmf-bootstrap` | Namespace for SA, RBAC, and runner Jobs |
| `ansible_runner_service_account` | `ansible-runner` | SA every runner Job runs as |
| `ansible_runner_cluster_role` | `cluster-admin` | RBAC scope; tighten post-spike |
| `ansible_runner_image` | `quay.io/ansible/awx-ee:latest` | Container image for runner Jobs |
| `ansible_runner_image_pull_policy` | `IfNotPresent` | Pull policy |
