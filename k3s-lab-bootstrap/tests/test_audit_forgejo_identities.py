"""Fixture tests for the supported Forgejo full user-list CLI format."""

import importlib.util
import json
from pathlib import Path
import unittest

import yaml


PLAYBOOK = Path(__file__).parents[1] / "playbooks/audit-admin-identities.yml"
HELPER = Path(__file__).parents[1] / "playbooks/files/audit_forgejo_identities.py"
spec = importlib.util.spec_from_file_location("audit_forgejo_identities", HELPER)
audit_module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(audit_module)


CLI_FIXTURE = """ID    Username            Email                                  IsActive  IsAdmin  2FA
1     forgejo-break-glass  break-glass@example.invalid           true      true     true
2     forgejo-svc          forgejo-svc@example.invalid            true      false    false
3     operator-user        operator@example.invalid               true      true     true
4     contributor-user     contributor@example.invalid            true      false    false
"""


def ops_admins_output(names):
    """The ak shell stdout the playbook snippet produces, with startup noise."""
    return "\n".join([
        "2026-01-01 00:00:00 [info] starting authentik shell",
        "operator-noise-line",
        "DMF_OPS_ADMINS=" + json.dumps(names),
        "[info] shell closed",
    ])


def payload(output=CLI_FIXTURE, ops_admins=None):
    names = ["operator-user"] if ops_admins is None else ops_admins
    return {
        "users_output": output,
        "users_rc": 0,
        "ops_admins_rc": 0,
        "ops_admins_output": ops_admins_output(names),
    }


def privileged_output(prefix, names):
    """One app query's stdout: startup noise plus the sentinel payload line."""
    return "\n".join([
        "[info] starting shell",
        "awx-break-glass",
        prefix + json.dumps(names),
        "[info] shell closed",
    ])


def privileged_payload(awx=None, netbox=None, authentik=None, ops_admins=None):
    def entry(prefix, expected, names):
        return {
            "expected": expected,
            "rc": 0,
            "output": privileged_output(prefix, [expected] if names is None else names),
        }

    return {
        "check": "privileged",
        "ops_admins_rc": 0,
        "ops_admins_output": ops_admins_output(["operator-user"] if ops_admins is None else ops_admins),
        "awx": entry("DMF_AWX_SUPERUSERS=", "awx-break-glass", awx),
        "netbox": entry("DMF_NETBOX_SUPERUSERS=", "netbox-break-glass", netbox),
        "authentik": entry("DMF_AUTHENTIK_ADMINS=", "akadmin", authentik),
    }


class AuditForgejoIdentitiesTests(unittest.TestCase):
    def test_breakglass_service_and_oidc_admin_policy_passes(self):
        result = audit_module.audit(payload())
        self.assertTrue(result["passed"])
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["inventory"]["user_count"], 4)
        self.assertEqual(result["inventory"]["active_user_count"], 4)
        self.assertEqual(result["inventory"]["admin_count"], 2)

    def test_unexpected_admin_fails_even_when_expected_breakglass_is_present(self):
        extra_admin = "5     unexpected-admin     unexpected@example.invalid            true      true     false\n"
        result = audit_module.audit(payload(CLI_FIXTURE + extra_admin))
        self.assertFalse(result["passed"])
        self.assertIn("unexpected Forgejo administrator present", result["findings"])

    def test_expected_ops_admin_missing_admin_role_fails(self):
        output = CLI_FIXTURE.replace("operator@example.invalid               true      true", "operator@example.invalid               true      false")
        result = audit_module.audit(payload(output))
        self.assertFalse(result["passed"])
        self.assertIn("active expected ops-admin account lacks Forgejo administrator rights", result["findings"])

    def test_disabled_forgejo_user_is_not_required_to_carry_current_ops_admin_role(self):
        output = CLI_FIXTURE.replace("operator@example.invalid               true      true", "operator@example.invalid               false     false")
        result = audit_module.audit(payload(output))
        self.assertTrue(result["passed"])

    def test_breakglass_and_service_exception_are_checked(self):
        output = CLI_FIXTURE.replace("forgejo-break-glass  ", "removed-break-glass  ")
        result = audit_module.audit(payload(output))
        self.assertFalse(result["passed"])
        self.assertIn("expected break-glass account is missing", result["findings"])

        service_admin = CLI_FIXTURE.replace("true      false    false\n3", "true      true     false\n3")
        result = audit_module.audit(payload(service_admin))
        self.assertFalse(result["passed"])
        self.assertIn("Forgejo service account has administrator rights", result["findings"])

    def test_unknown_cli_format_fails_closed(self):
        result = audit_module.audit(payload(CLI_FIXTURE.replace("2FA", "TwoFA", 1)))
        self.assertFalse(result["passed"])
        self.assertIn("unsupported Forgejo CLI user-list format", result["findings"])

    def test_empty_email_cell_is_supported_without_exposing_data(self):
        output = CLI_FIXTURE.replace("1     forgejo-break-glass  break-glass@example.invalid           true      true     true",
                                     "1     forgejo-break-glass  true      true     true")
        result = audit_module.audit(payload(output))
        self.assertTrue(result["passed"])

    def test_malformed_authentik_identity_input_fails_closed(self):
        result = audit_module.audit(payload(ops_admins=["", "operator-user"]))
        self.assertFalse(result["passed"])
        self.assertIn("Authentik ops-admin identity list is missing or malformed", result["findings"])

    def test_noise_around_the_payload_line_is_not_read_as_usernames(self):
        # "operator-noise-line" and the log lines in the fixture are not users.
        result = audit_module.audit(payload(ops_admins=["operator-user"]))
        self.assertTrue(result["passed"])
        self.assertEqual(result["findings"], [])
        names = audit_module._parse_ops_admins(ops_admins_output(["operator-user", "second-user"]))
        self.assertEqual(names, ["operator-user", "second-user"])
        self.assertEqual(audit_module._parse_ops_admins(ops_admins_output([])), [])

    def test_missing_payload_line_fails_closed(self):
        input_payload = payload()
        input_payload["ops_admins_output"] = "[info] starting authentik shell\noperator-user\n"
        result = audit_module.audit(input_payload)
        self.assertFalse(result["passed"])
        self.assertEqual(result["findings"], ["Authentik ops-admin output lacks exactly one payload line"])

    def test_duplicate_payload_line_fails_closed(self):
        input_payload = payload()
        line = "DMF_OPS_ADMINS=" + json.dumps(["operator-user"])
        input_payload["ops_admins_output"] = line + "\n" + line
        result = audit_module.audit(input_payload)
        self.assertFalse(result["passed"])
        self.assertEqual(result["findings"], ["Authentik ops-admin output lacks exactly one payload line"])

    def test_unparseable_payload_line_fails_closed(self):
        input_payload = payload()
        input_payload["ops_admins_output"] = "DMF_OPS_ADMINS=not-json"
        result = audit_module.audit(input_payload)
        self.assertFalse(result["passed"])
        self.assertEqual(result["findings"], ["Authentik ops-admin payload is not valid JSON"])

    def test_failed_authentik_query_fails_closed(self):
        input_payload = payload()
        input_payload["ops_admins_rc"] = 1
        result = audit_module.audit(input_payload)
        self.assertFalse(result["passed"])
        self.assertIn("Authentik ops-admin group query failed", result["findings"])

    def test_report_discards_emails_and_usernames_and_states_binding_gap(self):
        rendered = json.dumps(audit_module.audit(payload()))
        for private_value in (
            "break-glass@example.invalid",
            "operator@example.invalid",
            "contributor@example.invalid",
            "operator-user",
            "forgejo-break-glass",
        ):
            self.assertNotIn(private_value, rendered)
        self.assertIn("pending supported read-only source-key query", rendered)
        self.assertIn("not available from the supported CLI output", rendered)


APPS = {
    "awx": ("DMF_AWX_SUPERUSERS=", "awx-break-glass", "AWX superusers"),
    "netbox": ("DMF_NETBOX_SUPERUSERS=", "netbox-break-glass", "NetBox superusers"),
    "authentik": ("DMF_AUTHENTIK_ADMINS=", "akadmin", "Authentik Admins group"),
}


class PrivilegedIdentityTests(unittest.TestCase):
    def test_exact_approved_sets_pass(self):
        result = audit_module.audit_privileged(privileged_payload())
        self.assertTrue(result["passed"])
        self.assertEqual(result["findings"], [])

    def test_ops_admin_members_are_approved_in_awx_and_netbox(self):
        result = audit_module.audit_privileged(privileged_payload(
            awx=["awx-break-glass", "operator-user"],
            netbox=["netbox-break-glass", "operator-user"],
        ))
        self.assertTrue(result["passed"], result["findings"])

    def test_ops_admin_member_in_authentik_admins_is_a_finding(self):
        # The baseline keeps the ops user out of `authentik Admins`
        # (blueprints/15-ops-user-webauthn.yaml.j2:69).
        result = audit_module.audit_privileged(privileged_payload(
            authentik=["akadmin", "operator-user"],
        ))
        self.assertFalse(result["passed"])
        self.assertEqual(
            result["findings"],
            ["Authentik Admins group: unexpected privileged identity 'operator-user'"],
        )

    def test_noise_equal_to_expected_name_does_not_satisfy_the_check(self):
        for app, (prefix, expected, label) in APPS.items():
            with self.subTest(app=app):
                data = privileged_payload(**{app: ["someone-else"]})
                data[app]["output"] = "\n".join([expected, prefix + json.dumps(["someone-else"]), expected])
                result = audit_module.audit_privileged(data)
                self.assertFalse(result["passed"])
                self.assertIn(f"{label}: expected identity '{expected}' is not privileged or is missing", result["findings"])
                self.assertIn(f"{label}: unexpected privileged identity 'someone-else'", result["findings"])

    def test_extra_privileged_user_is_named_as_a_finding(self):
        for app, (_prefix, expected, label) in APPS.items():
            with self.subTest(app=app):
                result = audit_module.audit_privileged(privileged_payload(**{app: [expected, "rogue-admin"]}))
                self.assertFalse(result["passed"])
                self.assertEqual(result["findings"], [f"{label}: unexpected privileged identity 'rogue-admin'"])

    def test_authentik_breakglass_user_is_approved_when_listed(self):
        # The authentik role adds its dormant break-glass user to
        # `authentik Admins` on purpose.
        data = privileged_payload(authentik=["akadmin", "break-glass"])
        data["authentik"]["also_expected"] = ["break-glass"]
        result = audit_module.audit_privileged(data)
        self.assertTrue(result["passed"], result["findings"])
        self.assertEqual(result["findings"], [])

    def test_missing_required_identity_is_a_finding_for_every_app(self):
        for app, (_prefix, expected, label) in APPS.items():
            with self.subTest(app=app):
                result = audit_module.audit_privileged(privileged_payload(**{app: []}))
                self.assertFalse(result["passed"])
                self.assertEqual(
                    result["findings"],
                    [f"{label}: expected identity '{expected}' is not privileged or is missing"],
                )

    def test_removed_authentik_breakglass_user_is_a_finding(self):
        data = privileged_payload(authentik=["akadmin"])
        data["authentik"]["also_expected"] = ["break-glass"]
        result = audit_module.audit_privileged(data)
        self.assertEqual(
            result["findings"],
            ["Authentik Admins group: expected identity 'break-glass' is not privileged or is missing"],
        )

    def test_ops_admin_members_are_allowed_not_required(self):
        # SSO users appear at first login, so an absent ops-admin member is
        # not a finding in any app.
        result = audit_module.audit_privileged(privileged_payload(ops_admins=["never-logged-in"]))
        self.assertTrue(result["passed"], result["findings"])

    def test_authentik_breakglass_user_is_unexpected_when_not_listed(self):
        data = privileged_payload(authentik=["akadmin", "break-glass"])
        result = audit_module.audit_privileged(data)
        self.assertEqual(result["findings"], ["Authentik Admins group: unexpected privileged identity 'break-glass'"])

    def test_extra_user_beside_breakglass_still_fails_naming_the_extra(self):
        data = privileged_payload(authentik=["akadmin", "break-glass", "rogue-admin"])
        data["authentik"]["also_expected"] = ["break-glass"]
        result = audit_module.audit_privileged(data)
        self.assertFalse(result["passed"])
        self.assertEqual(result["findings"], ["Authentik Admins group: unexpected privileged identity 'rogue-admin'"])

    def test_malformed_also_expected_fails_closed(self):
        for bad in ("break-glass", [""], [1]):
            with self.subTest(bad=bad):
                data = privileged_payload()
                data["authentik"]["also_expected"] = bad
                self.assertFalse(audit_module.audit_privileged(data)["passed"])

    def test_playbook_passes_the_authentik_breakglass_name_to_the_helper(self):
        plays = yaml.safe_load(PLAYBOOK.read_text())
        task = next(
            t for play in plays for t in play["tasks"]
            if t.get("name") == "Parse and audit privileged identity sets"
        )
        stdin = task["ansible.builtin.command"]["stdin"]
        self.assertIn("'also_expected': [audit_expected_authentik_breakglass | trim]", stdin)
        self.assertIn("authentik_breakglass_is_active | default(true) | bool", stdin)
        self.assertIn(
            "authentik_breakglass_username | default('break-glass')",
            plays[0]["vars"]["audit_expected_authentik_breakglass"],
        )

    def test_missing_duplicate_or_malformed_payload_fails_closed(self):
        for app, (prefix, expected, label) in APPS.items():
            cases = {
                "missing": "[info] shell\n" + expected + "\n",
                "duplicate": (prefix + json.dumps([expected]) + "\n") * 2,
                "bad json": prefix + "not-json",
                "not a list": prefix + json.dumps({"name": expected}),
                "empty name": prefix + json.dumps([expected, ""]),
                "duplicate name": prefix + json.dumps([expected, expected]),
            }
            for case, output in cases.items():
                with self.subTest(app=app, case=case):
                    data = privileged_payload()
                    data[app]["output"] = output
                    result = audit_module.audit_privileged(data)
                    self.assertFalse(result["passed"])
                    self.assertEqual(len(result["findings"]), 1)
                    self.assertTrue(result["findings"][0].startswith(label), result["findings"])

    def test_failed_query_or_ops_admin_payload_fails_closed(self):
        for app in APPS:
            with self.subTest(app=app):
                data = privileged_payload()
                data[app]["rc"] = 1
                self.assertFalse(audit_module.audit_privileged(data)["passed"])
        data = privileged_payload()
        data["ops_admins_output"] = "operator-user"
        self.assertFalse(audit_module.audit_privileged(data)["passed"])
        data = privileged_payload()
        data["ops_admins_rc"] = 1
        self.assertFalse(audit_module.audit_privileged(data)["passed"])

    def test_skipped_app_is_not_checked_but_others_still_are(self):
        data = privileged_payload(netbox=["netbox-break-glass", "rogue-admin"])
        data["awx"] = {"skipped": True}
        result = audit_module.audit_privileged(data)
        self.assertEqual(result["findings"], ["NetBox superusers: unexpected privileged identity 'rogue-admin'"])

    def test_playbook_queries_print_the_sentinel_lines_the_helper_reads(self):
        text = PLAYBOOK.read_text()
        for prefix, _expected, _label in APPS.values():
            self.assertEqual(text.count("'" + prefix + "'"), 1, prefix)


CONTROLLER_TASKS = {
    "Parse and audit Forgejo identities without exposing profile fields",
    "Parse and audit privileged identity sets",
}


def all_tasks(plays):
    """Every task in the plays, including those nested in blocks and handlers."""
    def walk(tasks):
        for task in tasks or []:
            yield task
            for key in ("block", "rescue", "always"):
                yield from walk(task.get(key))

    for play in plays:
        for section in ("pre_tasks", "tasks", "post_tasks", "handlers"):
            yield from walk(play.get(section))


def controller_task_problems(plays):
    """Return what is wrong with delegated tasks and task names, if anything."""
    tasks = list(all_tasks(plays))
    names = [task.get("name") for task in tasks]
    problems = [f"duplicate task name: {n}" for n in sorted({n for n in names if n and names.count(n) > 1})]
    delegated = set()
    for task in tasks:
        if "delegate_to" not in task:
            continue
        delegated.add(task.get("name"))
        if task["delegate_to"] != "localhost" or task.get("become") is not False:
            problems.append(f"delegated task needs localhost and become: false: {task.get('name')}")
    if delegated != CONTROLLER_TASKS:
        problems.append(f"unexpected delegated task set: {sorted(map(str, delegated))}")
    return problems


class PlaybookBecomeTests(unittest.TestCase):
    def test_controller_tasks_turn_become_off_and_nothing_else_delegates(self):
        # Play-level become makes a local run try sudo, which fails on an
        # operator workstation ("Premature end of stream waiting for become
        # success").
        self.assertEqual(controller_task_problems(yaml.safe_load(PLAYBOOK.read_text())), [])

    def test_a_duplicate_named_delegated_task_without_become_off_is_caught(self):
        # A name-keyed check lets a later task hide an earlier one; this
        # appends a same-named task that would run under play-level become.
        plays = yaml.safe_load(PLAYBOOK.read_text())
        plays[0]["tasks"].append({
            "name": "Parse and audit privileged identity sets",
            "ansible.builtin.command": "true",
            "delegate_to": "localhost",
        })
        problems = controller_task_problems(plays)
        self.assertTrue(any("duplicate task name" in p for p in problems), problems)
        self.assertTrue(any("become: false" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
