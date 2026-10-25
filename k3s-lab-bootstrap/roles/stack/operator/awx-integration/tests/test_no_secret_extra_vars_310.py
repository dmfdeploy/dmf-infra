"""Guard for dmfdeploy/dmfdeploy#310: no secret in a job template's extra_vars.

AWX returns a job template's extra_vars in clear on every read and in every
launch response. A secret must reach a launcher through an AWX credential
(see awx_integration_netbox_token_credentials), never through extra_vars.

This test reads the role defaults as data and fails when any job-template
definition puts a secret-shaped variable into extra_vars. It also fails when
the role tasks send a job template's extra_vars from anything other than the
reviewed job-template definitions.

A second check covers optional secret credentials. When an optional value
is empty, the run must detach the credential from the job templates that
declare it and delete it, so a value set earlier and later cleared is not
injected again.

Run from the repo root:  python3 -m unittest discover -s \
  k3s-lab-bootstrap/roles/stack/operator/awx-integration/tests -v
"""
import pathlib
import re
import unittest

import yaml

ROLE = pathlib.Path(__file__).resolve().parent.parent
SECRET_NAME = re.compile(r"token|password|passwd|secret|^vault_", re.IGNORECASE)
# A value that renders a secret-shaped variable, for example "{{ vault_x }}".
SECRET_VALUE = re.compile(r"token|password|passwd|secret|vault_", re.IGNORECASE)
# The only extra_vars expression the tasks may send to a job template. It
# renders the token-free definitions the first test checks.
ALLOWED_JT_BODY = '"{{ jt_item.extra_vars | default({}) | to_json }}"'
# The credential-type injector is not a job template; it carries a template
# placeholder, not a value.
ALLOWED_INJECTOR = "\"{{ {item.var_name: '{{ token }}'} }}\""


def _walk(value, path):
    """Yield (path, key, value) for every key in a nested mapping or list."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield (path, str(key), inner)
            yield from _walk(inner, f"{path}.{key}")
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            yield from _walk(inner, f"{path}[{index}]")


def secret_findings(defaults):
    """Return a list of 'jt: reason' strings for secrets in extra_vars."""
    found = []
    for var_name, entries in defaults.items():
        if "job_template" not in var_name or not isinstance(entries, list):
            continue
        for entry in entries:
            jt = entry.get("name", "?")
            for path, key, inner in _walk(entry.get("extra_vars") or {}, "extra_vars"):
                if SECRET_NAME.search(key):
                    found.append(f"{jt}: {path}.{key} has a secret-shaped name")
                if isinstance(inner, str) and SECRET_VALUE.search(inner):
                    found.append(f"{jt}: {path}.{key} value references a secret-shaped variable")
    return found


class NoSecretInExtraVars(unittest.TestCase):
    def test_job_template_definitions_hold_no_secret(self):
        defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
        templates = [n for n in defaults if "job_template" in n]
        self.assertTrue(templates, "no job-template definitions found; the guard checks nothing")
        self.assertEqual(secret_findings(defaults), [])

    def test_tasks_send_only_reviewed_extra_vars(self):
        bodies = []
        for line in (ROLE / "tasks" / "main.yml").read_text().splitlines():
            match = re.match(r"^\s*extra_vars:\s*(.*)$", line)
            if match and match.group(1).strip():
                bodies.append(match.group(1).strip())
        self.assertTrue(bodies, "no extra_vars assignments found; the guard checks nothing")
        unexpected = [b for b in bodies if b not in (ALLOWED_JT_BODY, ALLOWED_INJECTOR)]
        self.assertEqual(unexpected, [])

    def test_guard_detects_a_planted_secret(self):
        planted = {
            "awx_catalog_job_templates": [
                {"name": "t", "extra_vars": {"vault_netbox_catalog_token": "x"}},
                {"name": "u", "extra_vars": {"ok": "{{ vault_something }}"}},
                {"name": "v", "extra_vars": {"admin_password": "x"}},
            ]
        }
        self.assertEqual(len(secret_findings(planted)), 3)


def _uri_tasks(tasks):
    """Yield every ansible.builtin.uri task as (task, uri_args)."""
    for task in tasks:
        args = task.get("ansible.builtin.uri") if isinstance(task, dict) else None
        if isinstance(args, dict):
            yield task, args


def inactive_cleanup_findings(defaults, tasks):
    """Return what is missing for the inactive optional credential cleanup.

    An optional secret credential that a run does not manage (its value is
    empty) must be detached from the job templates that declare it and then
    deleted, so a value set earlier and later cleared is not injected again.
    """
    found = []
    optional = [
        c["credential_name"]
        for c in defaults.get("awx_integration_netbox_token_credentials") or []
        if c.get("optional")
    ]
    if not optional:
        return ["no optional credential in awx_integration_netbox_token_credentials; the guard checks nothing"]
    declared = {
        name
        for jt in defaults.get("awx_catalog_job_templates") or []
        for name in jt.get("extra_credentials") or []
    }
    for name in optional:
        if name not in declared:
            found.append(f"{name}: optional but no job template declares it, so the detach loop never reaches it")

    selects = [
        t for t in tasks
        if isinstance(t, dict)
        and "_awx_inactive_secret_credential_names" in (t.get("ansible.builtin.set_fact") or {})
        and "optional" in str(t["ansible.builtin.set_fact"]["_awx_inactive_secret_credential_names"])
    ]
    if not selects:
        found.append("no set_fact derives _awx_inactive_secret_credential_names from the optional entries")

    detach = [
        t for t, a in _uri_tasks(tasks)
        if (a.get("body") or {}).get("disassociate") is True
        and "/credentials/" in str(a.get("url", ""))
        and "_awx_inactive_cred_ids" in str(t.get("when", ""))
        and "extra_credentials" in str(t.get("loop", ""))
    ]
    if not detach:
        found.append("no task disassociates the inactive credentials from the job templates that declare them")

    delete = [
        t for t, a in _uri_tasks(tasks)
        if str(a.get("method", "")).upper() == "DELETE"
        and "/credentials/" in str(a.get("url", ""))
        and "_awx_inactive_cred_ids" in str(t.get("loop", ""))
    ]
    if not delete:
        found.append("no task deletes the inactive credentials")

    asserts = [
        t for t in tasks
        if isinstance(t, dict)
        and "_awx_inactive_secret_credential_names" in str((t.get("ansible.builtin.assert") or {}).get("that", ""))
    ]
    if not asserts:
        found.append("no read-back assert that the inactive credentials are gone")
    return found


class InactiveOptionalCredentialCleanup(unittest.TestCase):
    def test_role_detaches_and_deletes_inactive_optional_credentials(self):
        defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        self.assertEqual(inactive_cleanup_findings(defaults, tasks), [])

    def test_guard_detects_missing_cleanup(self):
        defaults = {
            "awx_integration_netbox_token_credentials": [
                {"credential_name": "opt", "optional": True},
            ],
            "awx_catalog_job_templates": [{"name": "t", "extra_credentials": []}],
        }
        # Nothing declares "opt", and no cleanup task exists.
        self.assertEqual(len(inactive_cleanup_findings(defaults, [])), 5)


if __name__ == "__main__":
    unittest.main()
