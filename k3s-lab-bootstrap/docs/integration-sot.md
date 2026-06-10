# Integration: NetBox SoT + AWX + Monitoring

This reference outlines how the lab should integrate NetBox as the source of
truth, AWX as the automation engine, Prometheus/Grafana for monitoring, and
LibreNMS for discovery/assurance.

## Guiding principle

NetBox is the authoritative source of infrastructure data. Other systems read
from it or write back updates explicitly.

## Target flow

1. **Discovery (LibreNMS)** finds devices and validates reachability.
2. **NetBox (SoT)** stores canonical inventory and IPAM.
3. **AWX** pulls inventory from NetBox and executes automation.
4. **Prometheus/Grafana** monitor systems; alerts can open tickets or trigger
   AWX workflows.
5. **Forgejo** hosts automation playbooks and configuration repositories for AWX
   and other apps.

Forgejo repos for automation:
- `awx-automation` (AWX projects/playbooks)
- `app-configs` (shared config/templates)

## Roles and links (what talks to what)

- **NetBox** is the system of record for inventory/IPAM.
- **AWX** consumes NetBox inventory and runs playbooks from Forgejo.
- **Forgejo** hosts the Git repos that AWX projects pull from.
- **LibreNMS** discovers devices and can feed updates into NetBox (limited write).
- **Prometheus/Grafana** monitor; alerts can trigger AWX remediations.

## Integration points

### NetBox -> AWX

- Configure an AWX **Inventory Source** using the NetBox inventory plugin.
- AWX sync pulls hosts, groups, and variables from NetBox.
- Playbooks should treat NetBox data as read‑only unless explicitly updating it.

### Service accounts and API tokens

Use dedicated service users/tokens per integration (avoid admin accounts).

Naming convention:
- Service users: `<system>-svc` (e.g., `awx-svc`, `librenms-svc`, `forgejo-svc`)
- NetBox groups: `<system>-readonly` or `<system>-writer`
- Tokens: `{{ system }}-token-<purpose>`

Suggested roles:
- **NetBox**
  - `awx-netbox` token: read‑only inventory/IPAM (devices, sites, roles, IPs).
  - `librenms-netbox` token: limited write (interfaces, IPs, device status).
- **AWX**
  - Service account scoped to inventory sync and required job templates only.
- **Grafana**
  - Read‑only API key for dashboard access or embedding.
- **LibreNMS**
  - Limited user for API access if writing back to NetBox.
- **Forgejo**
  - `forgejo-svc` user with access to automation repos used by AWX and apps.

Pros: least privilege, auditability, easier rotation, lower blast radius.

Implementation in this repo:
- Playbook: `playbooks/40-netbox-sot.yml`
- Role: `roles/netbox-sot`
- Vault is updated automatically with:
  - `vault_netbox_admin_token`
  - `vault_netbox_awx_token`
  - `vault_netbox_librenms_token`
- Non-interactive run:
  `ansible-playbook playbooks/40-netbox-sot.yml --vault-password-file ~/.vault_pass`
- The playbook creates the NetBox admin token by exec'ing into the NetBox pod
  (basic auth to `/users/tokens/` returns 403).

### LibreNMS -> NetBox (optional but recommended)

- Use LibreNMS discovery to suggest new devices or update interface details.
- Push approved discoveries into NetBox via API.
- Keep NetBox authoritative; LibreNMS is a feeder, not a source of truth.

### Prometheus/Grafana -> AWX

- Use alert rules to trigger AWX workflows (webhooks) for remediation.
- Record automation actions back into NetBox where possible.

## Suggested stages

1. **Inventory foundation**
   - Model sites/roles/devices/IPs in NetBox.
   - Ensure NetBox API tokens and roles are in vault.

2. **AWX inventory**
   - Add NetBox inventory source in AWX.
   - Validate group/host variables map correctly.

3. **Monitoring**
   - Register targets in Prometheus (static or via service discovery).
   - Map Grafana dashboards to NetBox metadata (site, role, device type).

4. **Discovery feedback**
   - Enable LibreNMS discovery.
   - Push discovered devices or interface updates into NetBox via API.

Run `playbooks/40-netbox-sot.yml` after the base cluster and application
playbooks have completed successfully.

## Playbook numbering and integration plan

Numbered playbooks are executed in order; higher numbers assume earlier ones
have already completed. The integration playbooks continue after core app
deployments are done:

- `playbooks/31-forgejo.yml` deploys the Forgejo application itself at
  `https://<forgejo-host>/`.
- `playbooks/40-netbox-sot.yml` creates NetBox service accounts/groups and
  stores v2 API tokens in `vault.yml`.
- `playbooks/41-forgejo-bootstrap.yml` creates the Forgejo service user,
  admin token, and repos for AWX projects/configs.
- `playbooks/42-awx-integration.yml` configures AWX to use Forgejo projects and
  NetBox inventory sources.
- `playbooks/43-librenms-integration.yml` configures LibreNMS to sync to NetBox.
- `playbooks/44-prometheus-integration.yml` wires Prometheus alerting into AWX.
- `playbooks/45-grafana-integration.yml` configures Grafana data sources and
  alert webhooks (if used).

Detailed plan (future reference):
1. Run `playbooks/31-forgejo.yml` to deploy Forgejo and publish
   `https://<forgejo-host>/`.
2. Run `playbooks/40-netbox-sot.yml` to create NetBox service accounts/tokens.
3. Run `playbooks/41-forgejo-bootstrap.yml` to create Forgejo repos/tokens.
4. Run `playbooks/42-awx-integration.yml` to configure AWX:
    - Create a project pointing at Forgejo `awx-automation`.
    - Add the NetBox inventory source using an SCM inventory file in Forgejo.
    - Add `collections/requirements.yml` so AWX can install `netbox.netbox`.
    - Create job templates that reference playbooks in Forgejo.
5. Run `playbooks/43-librenms-integration.yml` to configure LibreNMS:
   - Use the NetBox writer token (`vault_netbox_librenms_token`) for sync.
6. Run `playbooks/44-prometheus-integration.yml`:
   - Wire alerts to AWX webhooks for remediation (optional).
7. Run `playbooks/45-grafana-integration.yml`:
   - Configure data sources/dashboards and alert webhooks (optional).

Forgejo deployment:
- Playbook: `playbooks/31-forgejo.yml`
- Role: `roles/stack/operator/forgejo`
- Verification gate:
  - `https://<forgejo-host>/`
  - `https://<forgejo-host>/api/v1/version`

Forgejo bootstrap:
- Playbook: `playbooks/41-forgejo-bootstrap.yml`
- Role: `roles/forgejo-bootstrap`
- Vault keys:
  - `vault_forgejo_admin_token`
  - `vault_forgejo_svc_password`
  - `vault_forgejo_svc_token`
- Non-interactive run:
  `ansible-playbook playbooks/41-forgejo-bootstrap.yml --vault-password-file ~/.vault_pass`

AWX integration:
- Playbook: `playbooks/42-awx-integration.yml`
- Role: `roles/awx-integration`
- Vault keys:
  - `vault_awx_svc_password`
  - `vault_awx_svc_token`

## Notes

- Avoid having multiple tools write the same fields unless ownership is defined.
- Prefer manual approval for new devices before writing to NetBox.
- See `docs/netbox-token-journey.md` for the NetBox admin token workflow.
- See `docs/awx-integration-plan.md` for the AWX integration plan.
