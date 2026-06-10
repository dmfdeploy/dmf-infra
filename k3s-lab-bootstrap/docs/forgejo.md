# Forgejo on a Dedicated Host

Forgejo is served behind Traefik on its own host, typically
`https://forgejo.<cluster-domain>/`, using a standard Kubernetes `Ingress`.

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
