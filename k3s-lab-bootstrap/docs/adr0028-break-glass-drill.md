# ADR-0028 Break-Glass Drill

This runbook is the D8 operational drill for the identity and authority model.
Run it monthly per environment, rotating through applications so every local
break-glass path is exercised before it is needed during an incident.

## Scope

The drill proves three things:

1. The operator can recover access when OIDC is unavailable.
2. The tested local account is not needed for routine work.
3. The credential is rotated or explicitly re-sealed after use.

Use a short-lived test env first when changing the procedure. On persistent
environments, pick one application per month and keep the rest untouched.

## Procedure

1. Record the environment, date, operator, application, and reason:
   `ADR-0028 D8 monthly break-glass drill`.
2. Confirm normal OIDC access works for the operator passkey identity.
3. In a private browser profile, exercise the selected local break-glass login.
   Do not use the local account for any routine operation beyond a minimal
   authenticated read.
4. Confirm the local account has the expected emergency role and no unexpected
   routine activity is needed.
5. Rotate or re-seal the credential for the tested application:
   - Authentik and Zot: rerun the OpenBao-backed app-admin materialization path
     or rotate the OpenBao secret and reapply the owning playbook.
   - AWX, NetBox, and Forgejo: follow the current app-specific admin resolution
     path until the helper writer-side migrations land.
   - Grafana: follow the current ADR-0028 sanctioned-exception decision.
6. Re-test OIDC login and confirm the local account is no longer in use.
7. Write a drill record in the session log or ticket system.

## Drill Record

Use this shape so later audits can compare runs without reading prose:

```yaml
date: YYYY-MM-DD
environment: <env>
operator: <operator-identity>
application: <app>
local_identity: <app-break-glass-username>
oidc_precheck: pass|fail
local_login: pass|fail
credential_rotation_or_reseal: pass|fail|deferred
oidc_postcheck: pass|fail
notes: <short failure or deferral reason>
next_revisit: YYYY-MM-DD
```

A failed drill is not a paperwork failure. Treat it as an incident rehearsal:
keep the environment stable, document the exact break point, and fix the
smallest path that restores emergency access without making local accounts
routine-use accounts.
