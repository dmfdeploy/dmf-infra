# NetBox Admin Token Journey (v2 Tokens)

This note captures how we stabilized NetBox admin token creation for the
automation playbooks.

## What we observed

- NetBox v4.x rejects short v1 tokens for API auth (`Invalid v1 token`).
- The DB column `Token.key` is short (12 chars) by design. It is **not** the
  full token string.
- The full v2 token is composed as:
  `TOKEN_PREFIX + key + "." + token_secret`
- NetBox only shows the full token once at creation time. It cannot be read
  back later from the DB.

## Working approach

We create tokens inside the NetBox pod using `manage.py shell`, capture the
full v2 token string, and store it in `vault.yml` for reuse.

Key details:
- Use `users.constants.TOKEN_PREFIX` and `token.token` to build the full token.
- Use `Authorization: Bearer <full-token>` for API calls.
- Only create tokens when the vault value is empty.

## Example (inside NetBox pod)

```bash
sudo k3s kubectl -n netbox exec deploy/netbox -- /bin/sh -c "cat <<'PY' | /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell | tail -n 1
from users.models import Token, User
from users.constants import TOKEN_PREFIX
user = User.objects.get(username=\"admin\")
token = Token.objects.create(user=user, description=\"netbox-sot-admin\")
print(f\"{TOKEN_PREFIX}{token.key}.{token.token}\")
PY"
```

## Playbook behavior

- `roles/netbox-sot` uses the pod exec to generate:
  - `vault_netbox_admin_token`
  - `vault_netbox_awx_token`
  - `vault_netbox_librenms_token`
- Tokens are stored once in `vault.yml` and reused on future runs.

## Born-inventory access path

- `roles/common/dmf-born-inventory` no longer reads the OpenBao root token.
- The OpenBao bootstrap now creates a dedicated AppRole for born-inventory.
- That AppRole only has `read` access to `secret/data/apps/netbox/runtime`.
- The AppRole `role_id` and `secret_id` are written to the local OpenBao
  break-glass JSON so the playbook can log in without broad privileges.
- A direct `dmf_born_inventory_netbox_admin_token` override still exists for
  manual recovery, but it is no longer the default path.

## Object permissions (NetBox v4.x)

The AWX inventory token needs NetBox object permissions to read inventory
endpoints. NetBox v4.x uses `ObjectPermission` records (not the Django auth
permissions list), and some API endpoints we expected (like
`/api/extras/object-types/`) are not available.

Working approach:
- Create/update `ObjectPermission` records via `manage.py` inside the pod.
- Attach permissions via `ObjectPermission.groups` (not `group.permissions`).
- Use ContentType model names that match NetBox (e.g., `devicetype`,
  `devicerole`, not `device_type`/`device_role`).

Minimal example (inside NetBox pod):
```bash
sudo k3s kubectl -n netbox exec deploy/netbox -- /bin/sh -c "cat <<'PY' | /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell
from django.contrib.contenttypes.models import ContentType
from users.models import ObjectPermission, Group

object_types = [
    'dcim.site',
    'dcim.device',
    'dcim.devicetype',
    'dcim.devicerole',
]
cts = [ContentType.objects.get(app_label=o.split('.')[0], model=o.split('.')[1]) for o in object_types]

perm, _ = ObjectPermission.objects.get_or_create(
    name='awx-readonly',
    defaults={'description': 'AWX inventory read-only access', 'actions': ['view']},
)
perm.actions = ['view']
perm.object_types.set(cts)
perm.save()

group = Group.objects.get(name='awx-readonly')
perm.groups.set([group])
PY"
```

## Gotchas

- A v1 token (short) will always fail against NetBox v4.x.
- You cannot recover a full v2 token once created; store it immediately.
- Ensure `API_TOKEN_PEPPERS` is configured (we already provide it via
  `netbox-extra-config`).
- AWX inventory sync via `netbox.netbox.nb_inventory` depends on the NetBox
  OpenAPI schema endpoint (`/api/schema/?format=json`), not just
  ordinary REST reads like `/netbox/api/status/`.
- **KNOWN BUG (NetBox v4.5.0 / drf-spectacular 0.29.0):** The schema
  generation was fundamentally broken due to a bug in `drf_spectacular/plumbing.py`
  line 1277 (`request.auth = original_request.auth` — WSGIRequest has no `.auth`).
  This caused the schema endpoint to return HTTP 200 with 0 bytes or hang
  indefinitely. Fixed by patching to `getattr(original_request, 'auth', None)`
  via a ConfigMap volume mount. The patch is now baked into the NetBox role.
- **INVENTORY TOKEN REQUIREMENT:** The `nb_inventory` plugin requires a valid
  NetBox API token in the `inventory/netbox.yml` file. Without it, the plugin
  gets 403 errors on all authenticated endpoints (`/api/dcim/devices/`, etc.)
  and fails with `'netbox-version'` KeyError. Ensure `691-netbox-sot.yml` is
  run before `693-awx-integration.yml` so the token exists in OpenBao.
- **API PATH NOTE:** The NetBox Docker image serves the API at `/api/` (not
  `/netbox/api/`). The `/netbox/` prefix is only added by the external
  Traefik IngressRoute. Internal cluster communication uses the root path.
- **Operational implication:**
  - do not treat an AWX NetBox inventory timeout as automatic proof of broken
    service discovery or pod networking
  - set an explicit `timeout` in the generated `inventory/netbox.yml`
  - if the schema path still fails with a larger timeout, debug NetBox schema
    generation directly rather than retrying AWX blind
