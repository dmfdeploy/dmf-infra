# Forgejo on a Dedicated Host

> **⚠️ Numbering/commands may be historical.** Parts of this document reference an
> earlier playbook-numbering scheme (e.g. `31-forgejo`, `40-netbox-sot`, `05-harden`)
> and the pre-OpenBao `--vault-password-file` workflow. The current tree uses the
> `200/300/600` + `vertical-*` layout and the `dmf-env/bin/run-playbook.sh` OpenBao
> wrapper. Cross-check against the live `k3s-lab-bootstrap/playbooks/` tree before running.

Forgejo is served behind Traefik on its own host, typically
`https://forgejo.<cluster-domain>/`, using a standard Kubernetes `Ingress`.

## Authentik access and contributor identity

The Authentik Forgejo application is bound to `forgejo-contributor` and
`ops-admin`. Membership grants login eligibility; it does not grant repository
write access, organisation ownership, or instance administration. Maintainers
grant those separately through Forgejo organisations, teams, and protected
branch review rules.

Forgejo uses Authentik's UUID-backed OIDC subject as the external user ID and
`userid` as its username source. OAuth account linking is disabled, so a
matching email address cannot merge a new external identity into an existing
account. Local registration and generic OpenID signup are disabled while OAuth
auto-registration remains enabled for the Authentik source. Authentik's
application binding is the eligibility gate for that source.

The evidence chain for a contribution is the Authentik subject and group
decision, the Forgejo account and repository events, and the pull-request
review and protected-branch history. Granting a team or repository permission
is a separate, attributable action after the account is provisioned.

## Design

- The upstream Forgejo Helm chart is vendored under `charts/forgejo/`.
- The role renders env-specific values and stages the chart on the target node
  before running Helm.
- Traefik handles HTTPS at the cluster edge.
- Forgejo itself is configured with:
  - `ROOT_URL = https://<forgejo-host>/`
  - host-root ingress at `/`
  - no path-rewrite middleware

## Playbook

```bash
ansible-playbook playbooks/31-forgejo.yml --vault-password-file ~/.vault_pass
```

## Verification

```bash
curl -I https://forgejo.<cluster-domain>/
curl https://forgejo.<cluster-domain>/api/v1/version
sudo k3s kubectl get pods -n forgejo
sudo k3s kubectl get pvc -n forgejo
sudo k3s kubectl get ingress -n forgejo
```

`41-forgejo-bootstrap.yml` should only run after the UI and API are both live.
