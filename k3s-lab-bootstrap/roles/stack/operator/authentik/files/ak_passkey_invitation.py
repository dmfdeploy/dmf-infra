# DMF bootstrap passkey invitation — ensure-or-mint logic.
#
# Canonical source for both:
#   - roles/stack/operator/authentik/tasks/main.yml (the 110-authentik play)
#   - dmf-env/bin/get-passkey-enrollment-url.sh (self-healing operator script)
#
# Executed inside the Authentik server pod via `ak shell -c "$(cat …)"`.
# Reads every parameter from environment variables; emits a single JSON
# document on stdout describing the operator user, confirmed-passkey count,
# and (when the requirement is not yet met) the current reusable
# enrollment URL (within its TTL window) plus its expiry. Read-only when the requirement is already
# met — the existing invitation, if any, is deleted; no URL is emitted.
#
# Required env vars:
#   AK_BOOTSTRAP_PASSKEY_USERNAME             operator username
#   AK_BOOTSTRAP_PASSKEY_EMAIL                operator email (also identifies user)
#   AK_BOOTSTRAP_PASSKEY_NAME                 operator display name
#   AK_BOOTSTRAP_PASSKEY_FLOW                 enrollment flow slug
#   AK_BOOTSTRAP_PASSKEY_INVITATION           invitation `name` field (lookup key)
#   AK_BOOTSTRAP_PASSKEY_TTL_HOURS            invitation TTL, integer hours
#   AK_BOOTSTRAP_PASSKEY_MIN_CONFIRMED_DEVICES  passkey count required (ADR-0028 D8: ≥2)
#   AK_AUTHENTIK_PUBLIC_URL                   public base URL (used to build the link)
#
# Output JSON keys (all stringified by the caller as needed):
#   changed, has_webauthn, passkey_requirement_met,
#   webauthn_count, required_webauthn_count,
#   username, email, url, expires,
#   existing_devices  — list of {name, aaguid} for confirmed WebAuthn
#                        devices currently registered on the user;
#                        consumed by dmf-env/bin/get-passkey-enrollment-url.sh
#                        to surface authenticator diversity hints
#                        (ADR-0028 D8 / R2 of the 2026-05-28 policy
#                        alignment survey). aaguid is the WebAuthn
#                        authenticator-model identifier — same aaguid
#                        across two entries means same authenticator
#                        family (e.g. iCloud Keychain), which signals
#                        the next enrollment must pick a different
#                        authenticator to satisfy D8 diversity.
import json
import os
from datetime import timedelta
from django.utils import timezone
from authentik.core.models import User
from authentik.flows.models import Flow
from authentik.stages.authenticator_webauthn.models import WebAuthnDevice
from authentik.stages.invitation.models import Invitation

username = os.environ["AK_BOOTSTRAP_PASSKEY_USERNAME"]
email = os.environ["AK_BOOTSTRAP_PASSKEY_EMAIL"]
display_name = os.environ["AK_BOOTSTRAP_PASSKEY_NAME"]
flow_slug = os.environ["AK_BOOTSTRAP_PASSKEY_FLOW"]
invitation_name = os.environ["AK_BOOTSTRAP_PASSKEY_INVITATION"]
public_url = os.environ["AK_AUTHENTIK_PUBLIC_URL"].rstrip("/")
ttl_hours = int(os.environ["AK_BOOTSTRAP_PASSKEY_TTL_HOURS"])
min_confirmed_devices = int(os.environ["AK_BOOTSTRAP_PASSKEY_MIN_CONFIRMED_DEVICES"])

user = User.objects.get(username=username, email=email)
flow = Flow.objects.get(slug=flow_slug)
akadmin = User.objects.filter(username="akadmin").first()
invitations = Invitation.objects.filter(name=invitation_name)
confirmed_devices = list(
    WebAuthnDevice.objects.filter(user=user, confirmed=True)
    .values("name", "aaguid")
)
existing_devices = [
    {"name": d["name"] or "", "aaguid": str(d["aaguid"] or "")}
    for d in confirmed_devices
]
webauthn_count = len(confirmed_devices)
has_webauthn = webauthn_count > 0
passkey_requirement_met = webauthn_count >= min_confirmed_devices
result = {
    "changed": False,
    "has_webauthn": has_webauthn,
    "passkey_requirement_met": passkey_requirement_met,
    "webauthn_count": webauthn_count,
    "required_webauthn_count": min_confirmed_devices,
    "existing_devices": existing_devices,
    "username": username,
    "email": email,
    "url": "",
    "expires": "",
}

if passkey_requirement_met:
    deleted, _ = invitations.delete()
    result["changed"] = deleted > 0
else:
    now = timezone.now()
    invitation = invitations.order_by("-expires").first()
    expired = bool(invitation and invitation.expiring and invitation.expires and invitation.expires <= now)
    if invitation is None or expired:
        invitations.delete()
        invitation = Invitation.objects.create(
            name=invitation_name,
            flow=flow,
            single_use=False,
            expiring=True,
            expires=now + timedelta(hours=ttl_hours),
            created_by=akadmin,
            fixed_data={
                "username": username,
                "email": email,
                "name": display_name,
            },
        )
        result["changed"] = True
    else:
        changed = False
        if invitation.flow_id != flow.pk:
            invitation.flow = flow
            changed = True
        if invitation.single_use:
            invitation.single_use = False
            changed = True
        if not invitation.expiring:
            invitation.expiring = True
            changed = True
        fixed_data = {
            "username": username,
            "email": email,
            "name": display_name,
        }
        if invitation.fixed_data != fixed_data:
            invitation.fixed_data = fixed_data
            changed = True
        if changed:
            invitation.save()
            result["changed"] = True
    result["url"] = f"{public_url}/if/flow/{flow.slug}/?itoken={invitation.invite_uuid}"
    result["expires"] = invitation.expires.isoformat() if invitation.expires else ""

print(json.dumps(result, sort_keys=True))
