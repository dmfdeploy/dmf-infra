# charts/netbox — reference wrapper chart

Wraps the upstream `netbox-community/netbox` chart (pinned in `Chart.yaml`) so installs are reproducible and flypack-deployable without network access to `netbox-community.github.io`.

The subchart tarball under `charts/` is vendored (committed to git) after running `helm dependency update` once. Re-run that command whenever the pinned version changes; commit the resulting `Chart.lock` and `charts/netbox-<version>.tgz`.

## Values

Static values here are intentionally empty (`netbox: {}`). Environment-specific values are produced by `roles/stack/operator/netbox/templates/values.yml.j2` and passed to Helm at install time.

## Install (called from Ansible role)

The role runs:

```bash
helm upgrade --install netbox \
  {{ playbook_dir }}/../charts/netbox \
  -n netbox --create-namespace \
  -f /tmp/netbox-values.yml
```

## Refresh dependencies

```bash
cd k3s-lab-bootstrap/charts/netbox
helm dependency update
git add Chart.lock charts/netbox-*.tgz
```

## Why a wrapper instead of `chart_ref: netbox-community/netbox`?

- Pins the version in code reviewable in git diffs.
- Enables `helm lint` and `helm template` in CI without needing the upstream repo reachable.
- Makes offline (flypack) installs possible by committing the vendored subchart.
- Establishes a pattern every other stack component can follow.
