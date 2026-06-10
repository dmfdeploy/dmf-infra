# LibreNMS

LibreNMS is deployed on the host-root URL `https://librenms.dmf.example.com/`
behind the private Traefik lane.

## Authentication

LibreNMS now uses Authentik Socialite OIDC:

- provider package: `socialiteproviders/authentik`
- Socialite key: `authentik`
- callback: `/auth/authentik/callback`
- auto-redirect: enabled
- registration: enabled
- admin mapping: Authentik group `ops-admin` maps to LibreNMS role `admin`

The login flow is configured from the playbook, not manually in the UI.

## Role wiring

Files:

- `roles/modules/infra-monitoring/librenms/defaults/main.yml`
- `roles/modules/infra-monitoring/librenms/tasks/main.yml`
- `roles/modules/infra-monitoring/librenms/templates/values.yml.j2`

The role:

- reads the LibreNMS OIDC client credentials from the Authentik provider
- installs the Authentik Socialite provider package into the running LibreNMS frontend
- writes the `auth.socialite` config entries
- restarts the LibreNMS frontend so the provider is loaded

## Verification

```bash
curl -I https://librenms.dmf.example.com/login
```

Expected result:

- `302` redirect to `https://auth.dmf.example.com/application/o/authorize/...`

## Notes

- The local LibreNMS admin bootstrap is still present as a break-glass path.
- The OIDC login depends on the provider package being installed in the live pod.
- If the pod is recreated outside the playbook, rerun `62-librenms.yml` to restore the plugin and config.
