# authentik

First-pass Authentik operator role for the in-cluster OpenBao + ESO topology.

What this role does today:

- consumes `common/app-admin-facts` output for the permanent `akadmin` bootstrap
  credential and the separate break-glass credential
- seeds runtime secrets in OpenBao at `secret/apps/authentik/runtime`
- materializes runtime and break-glass Kubernetes Secrets through ESO
- mounts a repo-backed blueprint ConfigMap into Authentik
- applies an initial baseline blueprint pack for core groups via the worker CLI
- configures passkey-first/passwordless human login and creates a reusable
  bootstrap passkey enrollment invitation for the configured operator user until
  the ADR-0028 D8 minimum confirmed passkey count is met
- sets explicit OAuth2/OIDC provider token lifetimes: short access codes,
  15-minute access tokens, and shift-sized refresh tokens by default
- creates or updates the dormant local `break-glass` user inside Authentik and
  ensures membership in `break-glass` plus `authentik Admins`
- deploys the official `goauthentik/authentik` Helm chart
- creates a host-based Traefik `IngressRoute` at `auth.<cluster-domain>`

Normal human app access should start from the OpenBao-stored enrollment URL at
`secret/apps/authentik/bootstrap-passkey`. Reusable passwords are reserved for
`akadmin` and break-glass identities.

**Bootstrap passkey invitation — reusable within TTL (global behavior):**
The enrollment invitation (`single_use=False`) is reusable for its TTL window
(`authentik_bootstrap_passkey_invitation_ttl_hours`, default 24h) and survives
failed WebAuthn attempts so the operator can retry without a fresh URL. This is
a **global** role behavior (not sandbox-only) — it applies to every env that
runs the 110-authentik play. The TTL bounds the reuse window; after expiry or
passkey confirmation the invitation is cleaned up. Acceptable for the operator
bootstrap link; break-glass and service accounts use separate credential paths.

ADR-0028 D8 defaults:

- `authentik_bootstrap_passkey_min_confirmed_devices: 2`
- `authentik_oidc_access_code_validity: minutes=1`
- `authentik_oidc_access_token_validity: minutes=15`
- `authentik_oidc_refresh_token_validity: hours=8`
