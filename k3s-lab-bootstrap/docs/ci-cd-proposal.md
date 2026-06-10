# CI/CD Proposal: Multi-Environment Workflow

> **⚠️ Numbering/commands may be historical.** Parts of this document reference an
> earlier playbook-numbering scheme (e.g. `31-forgejo`, `40-netbox-sot`, `05-harden`)
> and the pre-OpenBao `--vault-password-file` workflow. The current tree uses the
> `200/300/600` + `vertical-*` layout and the `dmf-env/bin/run-playbook.sh` OpenBao
> wrapper. Cross-check against the live `k3s-lab-bootstrap/playbooks/` tree before running.

This proposal outlines a pragmatic CI/CD approach to run dev, staging, and
production concurrently while keeping secrets handling clean and promotions
predictable.

## Goals

- Run dev, staging, and prod in parallel with minimal drift.
- Keep secrets out of git for staging/prod while preserving dev convenience.
- Make promotion from dev -> staging -> prod repeatable and low-risk.

## Repository layout (proposed)

```
inventories/
  common/
    group_vars/all/main.yml     # shared defaults
  dev/
    hosts.ini
    group_vars/all/main.yml     # dev overrides
    group_vars/all/vault.yml    # dev secrets (ansible-vault)
  staging/
    hosts.ini
    group_vars/all/main.yml     # staging overrides
  prod/
    hosts.ini
    group_vars/all/main.yml     # prod overrides
```

Notes:
- `inventories/common` holds shared defaults used by all environments.
- Each environment overrides only what differs.

## Secrets strategy

- **Dev**: keep `vault.yml` in repo (ansible-vault).
- **Staging/Prod**: do not store secrets in repo.
  - Use external secret manager (HashiCorp Vault, 1Password, AWS/GCP secrets).
  - Inject secrets at runtime via `lookup()` plugins or CI secret env vars.

Guardrails:
- Disable playbook tasks that write back to `vault.yml` outside dev.
- Use an `env_name` variable to gate secret creation or updates.

## CI/CD pipeline outline

### Triggers
- Feature branches -> Dev pipeline
- Merge to `develop` -> Staging pipeline
- Merge to `main` -> Production pipeline

### Stages
1. **Validate**
   - `ansible-lint`, yamllint, and syntax check
2. **Plan/Preview**
   - `ansible-playbook --check --diff`
3. **Apply**
   - `ansible-playbook` with target inventory
4. **Verify**
   - smoke tests (HTTP checks, readiness, API health)

### Promotion flow

1) Dev branch merged -> Staging pipeline runs
2) Manual approval gate
3) Staging success -> Merge to main -> Prod pipeline runs

## Inventory usage in CI

Use layered inventory to avoid duplication:

```
ansible-playbook -i inventories/common -i inventories/dev playbooks/...
ansible-playbook -i inventories/common -i inventories/staging playbooks/...
ansible-playbook -i inventories/common -i inventories/prod playbooks/...
```

## Required repo changes (minimal)

- Add inventories for staging/prod and shared defaults in `inventories/common`.
- Introduce `env_name` in each environment.
- Gate secret-writing tasks behind `env_name == 'dev'` or a flag like
  `allow_vault_writes`.
- Keep `ansible.cfg` default inventory for local dev, but override it in CI
  via `ANSIBLE_INVENTORY` or `-i`.

## Operational guidance

- Keep environments isolated (separate k3s clusters or namespaces).
- Avoid manual drift: all changes go through playbooks and CI.
- Tag releases for prod and use the tag in the prod pipeline for traceability.
- Use dev as the early warning system:
  - Nightly job to pull upstream Helm charts/images.
  - Deploy into a disposable namespace/cluster.
  - Run smoke tests and alert on failures.
  - Promote only verified chart versions to the local registry for staging/prod.

### Example: Nightly dev validation job

Pseudo-steps for a nightly dev pipeline:

```bash
# 1) Refresh upstream chart versions (values can be pinned after validation)
helm repo add netbox https://netbox-community.github.io/netbox-chart/
helm repo add awx https://ansible.github.io/awx-operator/
helm repo update

# 2) Deploy to an ephemeral namespace
NAMESPACE=dev-validate-$(date +%Y%m%d)
kubectl create ns "$NAMESPACE"

# 3) Run the playbooks targeting that namespace/inventory
ansible-playbook -i inventories/common -i inventories/dev playbooks/30-netbox.yml -e landing_page_namespace="$NAMESPACE"
ansible-playbook -i inventories/common -i inventories/dev playbooks/35-awx.yml -e awx_namespace="$NAMESPACE"

# 4) Smoke checks
curl -fsS http://<vip>/netbox/ || exit 1
curl -fsS http://<vip>/awx/ || exit 1

# 5) Cleanup
kubectl delete ns "$NAMESPACE"
```

If the job passes, promote the tested chart versions into the local registry
or a curated Helm repo for staging/prod.

## Next steps

1) Decide secret manager for staging/prod.
2) Create staging/prod inventories.
3) Add CI pipeline config (GitHub Actions, GitLab CI, or Forgejo Actions).
4) Add smoke tests for core services (AWX, NetBox, Forgejo).
