# AWX Integration Plan (NetBox + Forgejo)

> **⚠️ Numbering/commands may be historical.** Parts of this document reference an
> earlier playbook-numbering scheme (e.g. `31-forgejo`, `40-netbox-sot`, `05-harden`)
> and the pre-OpenBao `--vault-password-file` workflow. The current tree uses the
> `200/300/600` + `vertical-*` layout and the `dmf-env/bin/run-playbook.sh` OpenBao
> wrapper. Cross-check against the live `k3s-lab-bootstrap/playbooks/` tree before running.

This document captures the AWX integration plan and the rationale behind each
step so future agents can pick up the work quickly.

## Goal

Configure AWX to:
- use Forgejo as the source for playbooks/projects, and
- use NetBox as the inventory source of truth.

## Inputs and prerequisites

Required playbooks (already executed):
- `playbooks/40-netbox-sot.yml` (creates NetBox tokens/users and stores them in
  `vault.yml`)
- `playbooks/41-forgejo-bootstrap.yml` (creates Forgejo service user/token and
  repositories)

Required vault keys:
- `vault_netbox_awx_token` (NetBox token for AWX read-only access)
- `vault_forgejo_svc_token` (Forgejo token for repo access)
- `vault_awx_svc_password`, `vault_awx_svc_token` (created by AWX integration)

## Integration tasks (what we do and why)

1. **Create AWX service user**
   - Reason: AWX automation should not run under the admin user.
   - Target: `awx-svc` user (non-superuser).

2. **Create an AWX service token**
   - Reason: API-driven automation requires a durable token; avoid admin creds.
   - Saved to `vault.yml` so future playbooks can reuse it.

3. **Create Source Control credential (Forgejo)**
   - Reason: AWX projects must authenticate to Forgejo to pull playbooks.
   - Uses `forgejo-svc` token from vault.

4. **Create NetBox inventory plugin config (in Forgejo repo)**
   - Reason: AWX uses SCM inventory sources; the NetBox plugin config must live
     in the project repo (e.g., `inventory/netbox.yml`).
   - Note: token is embedded in the file for now; consider secure injection
     later if AWX supports env/credential injection for SCM inventory.
   - Operational note: the `netbox.netbox.nb_inventory` plugin fetches the
     NetBox OpenAPI schema before inventory sync. On the live DMF NetBox
     deployment (`v4.5.0`), that schema path is materially slower than normal
     API endpoints and can exceed the plugin's documented default timeout of
     `60` seconds, so the generated inventory file should set an explicit
     `timeout`.

5. **Add collection requirements**
   - Reason: the NetBox inventory plugin lives in the `netbox.netbox`
     collection. AWX must install it from `collections/requirements.yml`.
   - If project updates skip collection install, enable content sync in AWX
     (Settings -> Jobs) or use a custom Execution Environment that includes
     `netbox.netbox`.

6. **Create AWX Project**
   - Reason: Project points at Forgejo repo `awx-automation`.
   - Uses SCM credential for authentication.

7. **Create AWX Inventory**
   - Reason: Container for inventory sources (NetBox inventory plugin).

8. **Create Inventory Source (SCM)**
   - Reason: AWX can sync hosts/groups by running the NetBox inventory plugin
     from the project repo.
   - Uses `source: scm` with `source_project` and `source_path`.

9. **Optional sync steps**
   - `project` and `inventory source` updates can be triggered automatically.
   - Enable only when you want immediate sync in automated runs.

## Expected wiring

- Forgejo repo: `https://<forgejo-host>/forgejo-svc/awx-automation.git`
- NetBox URL: `http://<vip>/netbox` (embedded in inventory plugin config)
- AWX API base: `http://<vip>/awx/api/v2`

## Playbook implementation notes

Planned playbook: `playbooks/42-awx-integration.yml`
Role: `roles/awx-integration`

Key behaviors:
- Non-interactive; uses `~/.vault_pass`.
- Idempotent resource creation (`GET` then `POST`).
- Secrets stored in `vault.yml`.

## Troubleshooting checklist

- If AWX objects are missing, query the AWX API for each object by name.
- If AWX returns auth errors, verify the admin credentials and the AWX URL.
- If inventory source creation fails, validate the NetBox credential type and
  token format.
- If project sync fails, check Forgejo token and repo URL.
- If NetBox inventory sync fails while basic NetBox API checks still succeed,
  test the schema path separately:
  - `http://netbox.netbox.svc.cluster.local/netbox/api/status/`
  - `http://netbox.netbox.svc.cluster.local/netbox/api/schema/?format=json`
- On `2026-04-18`, the DMF Hetzner cluster showed:
  - `status/` returned `200` quickly from inside `awx-task`
  - `schema/?format=json` returned no bytes and still timed out after `120s`
  - NetBox pod logs emitted large volumes of schema-generation warnings from
    `filtersets.py` during the schema request
- Interpretation:
  - this is not enough evidence to call AWX or cluster networking broken
  - it does justify setting a larger plugin timeout in the generated inventory
    config
  - if the schema endpoint still does not complete with the larger timeout, the
    remaining issue is on the NetBox schema-generation path itself and needs a
    NetBox-side fix or version-specific workaround
