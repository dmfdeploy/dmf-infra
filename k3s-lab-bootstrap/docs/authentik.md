# Authentik Passwordless Bootstrap

DMF human login is passwordless from first operator use. The bootstrap operator
does not get a reusable password. A clean deployment must produce a one-time,
invitation-gated passkey enrollment URL; after that enrollment, OIDC/SAML app
logins use the registered passkey first.

## Contract

- The bootstrap operator user (configured via
  `authentik_bootstrap_passkey_username` in the private inventory) is
  pre-seeded as a member of `ops-admin`.
- That user must not depend on a password for first login.
- Authentik cannot pre-create a WebAuthn/passkey device server-side. A passkey
  must be enrolled from the operator's browser/authenticator.
- The deployment therefore creates a single-use bootstrap enrollment invitation
  and stores its URL in OpenBao at:

```text
secret/apps/authentik/bootstrap-passkey
```

- The enrollment URL uses the `dmf-bootstrap-passkey-enrollment` flow:
  invitation token, user identification, passkey setup, then user login.
- The normal login page is configured with `dmf-passkey-login` as the
  passwordless/passkey flow and enables WebAuthn autofill on the identification
  stage.
- Password login remains only for break-glass/local admin identities such as
  `break-glass` and `akadmin`.

## First Login

After `vertical-security/110-authentik.yml` or `site.yml` completes:

1. Read the one-time enrollment URL from OpenBao.
2. Open that URL in the browser that owns the target passkey.
3. Identify as the operator passkey identity defined in your environment's
   private inventory (`authentik_bootstrap_passkey_username` /
   `authentik_bootstrap_passkey_email`).
4. Enroll the passkey when Authentik prompts for it.
5. Use Grafana, NetBox, Forgejo, or AWX; Authentik should prompt for passkey
   login instead of a reusable password.

If the Authentik database was deleted, previous passkey registrations are gone.
That is expected WebAuthn behavior. Re-run the Authentik playbook to mint a new
single-use enrollment URL, then enroll a new passkey.

## Retrieve The Enrollment URL

The exact OpenBao root token location is environment-specific. With a root token
available, the value to retrieve is:

```bash
bao kv get -field=enrollment_url secret/apps/authentik/bootstrap-passkey
```

The same secret also records:

```text
username
email
has_webauthn
webauthn_count
required_webauthn_count
passkey_requirement_met
expires
```

When `passkey_requirement_met=true`, the bootstrap user already has the
required number of confirmed passkeys and the enrollment URL is
intentionally blank. `has_webauthn=true` is retained for backward
compatibility and only means at least one confirmed passkey exists.

## Verification

Use these read-only checks against the cluster:

```bash
kubectl -n authentik exec deploy/authentik-server -- ak shell -c \
  'from authentik.stages.authenticator_webauthn.models import WebAuthnDevice; print(WebAuthnDevice.objects.filter(user__username="<operator-username>", confirmed=True).count())'
```

Expected after enrollment:

```text
2
```

Check that app OIDC still redirects to Authentik:

```bash
curl -Ik https://grafana.dmf.example.com/login
curl -Ik https://netbox.dmf.example.com/oauth/login/oidc/
```

Expected behavior:

- Grafana redirects to `/login/generic_oauth`, then Authentik.
- NetBox redirects to Authentik's OAuth authorize endpoint.
- The Authentik page offers passkey login for the enrolled operator.
