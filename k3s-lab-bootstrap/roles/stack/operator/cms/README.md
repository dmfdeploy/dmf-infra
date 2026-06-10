# cms

**Scope:** Custom operator-facing CMS — placeholder; content lands from dmf-cms repo in Phase 2

## Authentik Dependency

This role depends on the Authentik stack being deployed first. It reads the
`DMF Console` OIDC provider from Authentik, patches the CMS runtime Secret with
the returned client secret, and expects the operator passkey bootstrap flow to
already exist.

Order of operations:
- Deploy Authentik and the `DMF Console` OIDC app/provider first.
- Ensure the Authentik passkey bootstrap flow for the operator user is ready.
- Then run the CMS role so it can fetch the OIDC credentials and deploy the app.

**Status:** STUB — not yet implemented.
See `dmfdeploy/docs/architecture/DMF Platform Plan.md` for strategic context.
