# Proposal: Central Authentication and Authorization (Passkeys + OIDC)

This proposal describes a centralized identity model for the lab so users can
register with passkeys (Face/Touch ID), admins can assign roles centrally, and
all apps share consistent access control.

## Goals

- One identity system for all apps (AWX, NetBox, Grafana, Forgejo, etc.).
- End-user onboarding via QR registration and passkeys.
- Centralized group/role assignment that maps to app permissions.
- Keep service accounts and automation tokens separate from human identities.

## Recommended architecture

1) **Identity Provider (IdP): Keycloak**
   - OIDC provider with WebAuthn/passkey support.
   - Supports group and role claims for authorization.
   - Can provide QR or device-assisted login flows.

2) **App protection**
   - Prefer native OIDC support in apps (Grafana, AWX, NetBox, Forgejo).
   - Otherwise, protect routes with an OIDC proxy (oauth2-proxy).

3) **Ingress authentication**
   - Use Traefik middleware or oauth2-proxy sidecar to enforce OIDC.
   - Add middleware on IngressRoute rules per app.

## User onboarding flow

1) User visits a registration page (Keycloak).
2) Admin approves or assigns user to groups (e.g., `awx-admin`).
3) Bootstrap user receives a one-time, invitation-gated passkey enrollment URL;
   no reusable bootstrap password is issued for normal human login.
4) User registers a passkey (WebAuthn).
5) User logs in to any app using OIDC/SAML with the passkey-first Authentik
   flow.

## Role and group mapping

Define centralized groups in Keycloak and map them to each app:

| Keycloak Group | App Role |
| --- | --- |
| `awx-admin` | AWX Admin |
| `awx-operator` | AWX Job Runner |
| `netbox-admin` | NetBox Admin |
| `netbox-readonly` | NetBox Read Only |
| `grafana-admin` | Grafana Admin |
| `grafana-viewer` | Grafana Viewer |
| `forgejo-admin` | Forgejo Admin |
| `forgejo-dev` | Forgejo Developer |

Each app either:
- consumes OIDC claims directly, or
- maps OIDC groups to its internal roles.

## Service accounts vs. humans

Keep automation and app integrations separate:
- **Service accounts** remain local to the app (API tokens).
- **Human users** use OIDC.
- Never reuse service credentials for OIDC.

## Implementation outline (playbooks)

1) **Keycloak deployment** (new playbook + role)
   - Namespace: `idp`
   - Ingress: `/auth` (or dedicated subdomain)
   - External DB (optional), persistent storage
   - Realm: `k3s-lab`

2) **OIDC client per app**
   - Create Keycloak clients for AWX, NetBox, Grafana, Forgejo.
   - Configure redirect URIs and scopes (`openid`, `profile`, `email`, `groups`).

3) **App configuration**
   - AWX: configure OIDC in `extra_settings`.
   - NetBox: enable social auth OIDC.
   - Grafana: configure auth.generic_oauth.
   - Forgejo: configure OIDC OAuth2 provider.

4) **Ingress protection**
   - Prefer native app OIDC login if supported.
   - Otherwise deploy oauth2-proxy in front of the app.

## Required data and secrets

- Keycloak admin credentials (vault/secret manager).
- OIDC client secrets for each app.
- JWKS or issuer URL for apps to validate tokens.

## Operational guidance

- Use a dedicated realm for the lab.
- Back up Keycloak database and export realm regularly.
- For staging/prod, use a managed IdP or HA Keycloak setup.

## Phased rollout

1) Deploy Keycloak and set up test clients.
2) Enable OIDC for one app (Grafana).
3) Roll out to NetBox and AWX.
4) Add oauth2-proxy for any app without OIDC.
5) Make OIDC the default login method.

## Open decisions

- Is a dedicated subdomain available for the IdP?
- Should QR registration be self‑service or admin‑approved?
- Should we enforce MFA for privileged groups?
