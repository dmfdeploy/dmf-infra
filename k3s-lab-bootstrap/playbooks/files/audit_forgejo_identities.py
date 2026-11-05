#!/usr/bin/env python3
"""Sanitize and audit the output of ``forgejo admin user list``.

The Forgejo CLI table is the only live query this helper consumes. It does not
expose auth-source IDs or external-login keys, so the report explicitly marks
OIDC binding inventory as pending instead of inferring it from usernames.
"""

from __future__ import annotations

import json
import sys
from typing import Any


USER_LIST_HEADER = ["ID", "Username", "Email", "IsActive", "IsAdmin", "2FA"]
# ``ak shell`` prints startup noise around its output, so the playbook snippet
# prints its payload as one JSON line with this prefix and nothing else is read.
OPS_ADMINS_PREFIX = "DMF_OPS_ADMINS="
# Privileged-identity queries for the other apps use the same framing.
PRIVILEGED_APPS = {
    "awx": {"prefix": "DMF_AWX_SUPERUSERS=", "label": "AWX superusers"},
    "netbox": {"prefix": "DMF_NETBOX_SUPERUSERS=", "label": "NetBox superusers"},
    "authentik": {"prefix": "DMF_AUTHENTIK_ADMINS=", "label": "Authentik Admins group"},
}


def _failure(message: str) -> dict[str, Any]:
    return {"passed": False, "findings": [message], "inventory": {}}


def _parse_users(output: Any) -> list[dict[str, Any]]:
    if not isinstance(output, str):
        raise ValueError("Forgejo CLI output must be text")
    lines = [line.split() for line in output.splitlines() if line.strip()]
    if not lines or lines[0] != USER_LIST_HEADER:
        raise ValueError("unsupported Forgejo CLI user-list format")

    users: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    seen_usernames: set[str] = set()
    for row in lines[1:]:
        # An empty email cell is collapsed by whitespace splitting, so the
        # supported CLI table has either six columns or five with that cell
        # empty. Any other arity is rejected rather than guessed.
        if len(row) == 6:
            active_text, admin_text, twofa_text = row[3], row[4], row[5]
        elif len(row) == 5:
            active_text, admin_text, twofa_text = row[2], row[3], row[4]
        else:
            raise ValueError("unsupported Forgejo CLI user-list row")
        try:
            user_id = int(row[0])
        except ValueError as exc:
            raise ValueError("unsupported Forgejo CLI user-list row") from exc
        username = row[1]
        if any(value not in {"true", "false"} for value in (active_text, admin_text, twofa_text)):
            raise ValueError("unsupported Forgejo CLI user-list flags")
        if user_id <= 0 or not username or user_id in seen_ids or username in seen_usernames:
            raise ValueError("duplicate or invalid Forgejo user record")
        seen_ids.add(user_id)
        seen_usernames.add(username)
        users.append({
            "username": username,
            "active": active_text == "true",
            "admin": admin_text == "true",
            "two_factor": twofa_text == "true",
        })
    return users


def _parse_identity_list(output: Any, prefix: str, label: str) -> list[str]:
    """Return the validated username list on the single ``prefix`` line.

    Every identity query prints its result as one sentinel-prefixed JSON list,
    so shell startup noise can never be read as a username. A missing or
    repeated payload line, invalid JSON, a non-list, an empty or non-string
    name, and duplicate names all raise ValueError, which callers report as a
    failed audit.
    """
    if not isinstance(output, str):
        raise ValueError(f"{label} output must be text")
    hits = [line for line in output.splitlines() if line.startswith(prefix)]
    if len(hits) != 1:
        raise ValueError(f"{label} output lacks exactly one payload line")
    try:
        names = json.loads(hits[0][len(prefix):])
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} payload is not valid JSON") from exc
    if not isinstance(names, list) or any(not isinstance(name, str) or not name for name in names):
        raise ValueError(f"{label} identity list is missing or malformed")
    if len(set(names)) != len(names):
        raise ValueError(f"{label} identity list contains duplicates")
    return names


def _parse_ops_admins(output: Any) -> list[str]:
    return _parse_identity_list(output, OPS_ADMINS_PREFIX, "Authentik ops-admin")


def audit_privileged(payload: Any) -> dict[str, Any]:
    """Compare each app's live privileged identities with its approved set.

    The comparison runs in both directions. An identity that is privileged
    but not approved is reported, and so is a required identity that is not
    privileged. Required identities are the app's ``expected`` account and
    any ``also_expected`` names, such as the Authentik break-glass user that
    the authentik role adds to ``authentik Admins``. Active ``ops-admin``
    members are allowed but not required: the AWX and NetBox SSO mappings
    create their users at first login, and the Authentik baseline does not
    add them to ``authentik Admins``. Any query failure or malformed payload
    fails the audit closed. Privileged usernames are named in findings on
    purpose; the audit exists to point at them.
    """
    if not isinstance(payload, dict):
        return _failure("audit input must be an object")
    if payload.get("ops_admins_rc") != 0:
        return _failure("Authentik ops-admin group query failed")
    try:
        ops_admins = set(_parse_identity_list(
            payload.get("ops_admins_output"), OPS_ADMINS_PREFIX, "Authentik ops-admin"))
    except ValueError as exc:
        return _failure(str(exc))

    findings: list[str] = []
    for app, spec in PRIVILEGED_APPS.items():
        entry = payload.get(app)
        if not isinstance(entry, dict):
            return _failure(f"{spec['label']} audit input is missing")
        if entry.get("skipped") is True:
            continue
        expected = entry.get("expected")
        if not isinstance(expected, str) or not expected:
            return _failure(f"{spec['label']} expected identity must be a non-empty string")
        also_expected = entry.get("also_expected", [])
        if not isinstance(also_expected, list) or any(not isinstance(name, str) or not name for name in also_expected):
            return _failure(f"{spec['label']} also-expected identities must be a list of non-empty strings")
        if entry.get("rc") != 0:
            return _failure(f"{spec['label']} privileged identity query failed")
        try:
            live = set(_parse_identity_list(entry.get("output"), spec["prefix"], spec["label"]))
        except ValueError as exc:
            return _failure(str(exc))
        required = [expected, *also_expected]
        for name in required:
            if name not in live:
                findings.append(f"{spec['label']}: expected identity '{name}' is not privileged or is missing")
        for name in sorted(live - ops_admins - set(required)):
            findings.append(f"{spec['label']}: unexpected privileged identity '{name}'")

    return {"passed": not findings, "findings": findings, "inventory": {}}


def audit(payload: Any) -> dict[str, Any]:
    """Return a sanitized exact-admin-set check and aggregate inventory.

    ``payload`` contains ``users_output`` from the supported full user-list
    CLI command, the Authentik ``ops-admin`` shell output (``ops_admins_output``,
    parsed from its sentinel line only), and the expected local
    break-glass/service usernames. Raw usernames are used only in memory and
    never returned; emails and profile fields are ignored at parse time.
    """
    if not isinstance(payload, dict):
        return _failure("audit input must be an object")
    if payload.get("users_rc") != 0:
        return _failure("Forgejo CLI user-list query failed")
    if payload.get("ops_admins_rc") != 0:
        return _failure("Authentik ops-admin group query failed")
    try:
        users = _parse_users(payload.get("users_output"))
    except ValueError as exc:
        return _failure(str(exc))

    try:
        ops_admins = _parse_ops_admins(payload.get("ops_admins_output"))
    except ValueError as exc:
        return _failure(str(exc))

    break_glass = payload.get("break_glass_username", "forgejo-break-glass")
    service_user = payload.get("service_username", "forgejo-svc")
    if not isinstance(break_glass, str) or not break_glass or not isinstance(service_user, str) or not service_user:
        return _failure("expected local identity names must be non-empty strings")

    by_username = {user["username"]: user for user in users}
    findings: list[str] = []
    if break_glass not in by_username:
        findings.append("expected break-glass account is missing")
    elif not by_username[break_glass]["admin"]:
        findings.append("expected break-glass account is not an administrator")
    if service_user not in by_username:
        findings.append("expected Forgejo service account is missing")
    elif by_username[service_user]["admin"]:
        findings.append("Forgejo service account has administrator rights")

    expected_admins = set(ops_admins) | {break_glass}
    actual_admins = {user["username"] for user in users if user["admin"]}
    if actual_admins - expected_admins:
        findings.append("unexpected Forgejo administrator present")
    active_expected_admins = {
        username for username in set(ops_admins)
        if username in by_username and by_username[username]["active"]
    }
    if active_expected_admins - actual_admins:
        findings.append("active expected ops-admin account lacks Forgejo administrator rights")

    return {
        "passed": not findings,
        "findings": findings,
        "inventory": {
            "user_count": len(users),
            "active_user_count": sum(user["active"] for user in users),
            "admin_count": len(actual_admins),
            "local_account_inventory": "not available from the supported CLI output",
            "oidc_binding_inventory": "pending supported read-only source-key query",
            "oidc_identity_mapping": "not inferred from username equality",
        },
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        result = _failure("audit input is not valid JSON")
    else:
        # "check": "privileged" selects the cross-app privileged-identity
        # comparison; the default is the Forgejo audit.
        if isinstance(payload, dict) and payload.get("check") == "privileged":
            result = audit_privileged(payload)
        else:
            result = audit(payload)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
