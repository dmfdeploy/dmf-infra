"""Static check: no HTTP call or role default turns certificate verification off.

A call that skips verification lets anyone on the path read what it sends and
answer in the server's place. Tasks reach for tokens and passwords through
variables, so this check does not try to tell a call that sends a secret from
one that does not. It flags every site that turns verification off.

The check parses every YAML file under `roles/`, `playbooks/`, `inventories/`
and `vars/` (test files excluded) and fails for each mapping key that:

* names a verification switch (`validate_certs`, `ssl_verify`, `verify_ssl`,
  `tls_verify`) and is `false`, evaluates to `false` through `default(false)`,
  or reads a variable whose default is `false`, or
* names a skip switch (`insecure_skip_verify`, `skip_tls_verify`) and is `true`.

It also fails for a command, script or template that passes `--tls-verify=false`,
`--dest-tls-verify=false`, `curl -k` or `--insecure`, `verify=False` or `insecure_skip_verify: true` (templates included).

A site that cannot verify goes on ALLOWED with a one-line reason. The key is the
file and the exact YAML path to the switch, such as
`[Check NetBox API root].ansible.builtin.uri.validate_certs`. A list item that
has a `name` appears as `[name]`; any other list item as `[index]`. A second
off switch next to an allowed one has another path, so it fails. Each entry
must match a real site and carry a reason, so a stale entry fails.

Coverage is bounded. The check reads only the files and patterns above. It does
not resolve a switch set in a group or host variable file outside the example
inventory, a value assembled by a filter other than `default`, or an HTTP client
that has no switch of this name.
"""

import re
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCANNED_DIRS = ("roles", "playbooks", "inventories", "vars")

VERIFY_SWITCH = re.compile(r"(validate_certs|ssl_verify|verify_ssl|tls_verify)$", re.I)
SKIP_SWITCH = re.compile(r"(insecure_skip_verify|skip_tls_verify)$", re.I)
OFF = re.compile(r"^\s*(false|no|off|0)\s*$", re.I)
ON = re.compile(r"^\s*(true|yes|on|1)\s*$", re.I)
DEFAULT_OFF = re.compile(r"default\(\s*(false|no|0)\s*\)", re.I)
CLI_FORMS = {
    "tls-verify=false": re.compile(r"--(dest-|src-)?tls-verify=false"),
    "curl -k": re.compile(r"\bcurl\b[^\n]*\s(-[a-zA-Z]*k[a-zA-Z]*|--insecure)\b"),
    "verify=False": re.compile(r"verify\s*=\s*False"),
    "insecure_skip_verify": re.compile(r"insecure_?skip_?verify\s*:\s*true", re.I),
}

# Sites that cannot verify: {(file relative to ROOT, enclosing task or variable): reason}.
# Add an entry only when the call cannot present or reach a certificate the
# node trusts, and say why. Do not widen trust to make a call pass.
ALLOWED = {
    ("roles/shared/gate-ingress-ready/tasks/main.yml",
     "[Gate: assert Traefik ingress route is live].ansible.builtin.uri.validate_certs"):
        "Route probe sends no secret and accepts any HTTP status; callers pass IP or VIP URLs.",
    ("playbooks/lifecycle/operate-health-probes.yml",
     "[Probe NetBox API endpoint].tasks.[Check NetBox API root].ansible.builtin.uri.validate_certs"):
        "Runs on the operator workstation, which has no local CA, and sends no secret; "
        "a caller can still set the variable to true.",
    ("roles/base/zot-mirror/defaults/main.yml", "zot_mirror_source_tls_verify"):
        "The in-cluster Zot source is plain HTTP, so no certificate exists to verify. "
        "This is not protected transport; the value only selects HTTP.",
    ("roles/base/blackbox-exporter/defaults/main.yml",
     "blackbox_modules.http_2xx_insecure.http.tls_config.insecure_skip_verify"):
        "Probe module for endpoints without a trusted certificate; it sends no credentials.",
}


def _is_off(key, value):
    if SKIP_SWITCH.search(key):
        return isinstance(value, bool) and value is True or (
            isinstance(value, str) and bool(ON.match(value))
        )
    if VERIFY_SWITCH.search(key):
        return isinstance(value, bool) and value is False or (
            isinstance(value, str) and bool(OFF.match(value) or DEFAULT_OFF.search(value))
        )
    return False


def _yaml_files(root):
    for scanned in SCANNED_DIRS:
        for path in sorted((root / scanned).rglob("*.yml")):
            if "/tests/" in str(path.relative_to(root)) or str(path.relative_to(root)).startswith("tests/"):
                continue
            yield path


def _walk(node, path, out, rel):
    """Collect every off switch with its exact YAML path."""
    if isinstance(node, list):
        for index, item in enumerate(node):
            label = item.get("name") if isinstance(item, dict) else None
            step = f"[{label}]" if isinstance(label, str) else f"[{index}]"
            _walk(item, path + [step], out, rel)
    elif isinstance(node, dict):
        for key, value in node.items():
            here = path + [str(key)]
            if _is_off(str(key), value):
                out.append((rel, ".".join(here), f"{key}: {value}", "key"))
            elif (VERIFY_SWITCH.search(str(key)) and not SKIP_SWITCH.search(str(key))
                  and isinstance(value, str)):
                # A switch read from a variable: judged once the off defaults are known.
                out.append((rel, ".".join(here), value, "ref"))
            _walk(value, here, out, rel)


def _scan(root):
    """Return (sites, defaults_off): every off site, plus names of off defaults."""
    sites, errors = [], []
    for path in _yaml_files(root):
        rel = str(path.relative_to(root))
        try:
            docs = list(yaml.safe_load_all(path.read_text()))
        except yaml.YAMLError as exc:
            errors.append(f"{rel}: not parseable, so not checked ({str(exc).splitlines()[0]})")
            continue
        for doc in docs:
            _walk(doc, [], sites, rel)
    # Variables that hold an off value, by their bare name (the last path step).
    off_vars = {p.split(".")[-1] for rel, p, _w, kind in sites if kind == "key"
                and ("/defaults/" in rel or "group_vars" in rel or rel.startswith("vars/"))}
    found = []
    for rel, ypath, what, kind in sites:
        if kind == "key":
            found.append((rel, ypath, what))
        elif any(re.search(r"\b" + re.escape(v) + r"\b", what) for v in off_vars):
            found.append((rel, ypath, what))
    for scanned in ("roles", "playbooks"):
        for path in sorted((root / scanned).rglob("*")):
            rel = str(path.relative_to(root))
            if (not path.is_file() or path.suffix not in (".yml", ".j2", ".sh", ".py")
                    or "/tests/" in rel):
                continue
            text = path.read_text(errors="replace")
            for label, pattern in CLI_FORMS.items():
                if label == "insecure_skip_verify" and path.suffix != ".j2":
                    continue  # YAML files are already checked by key
                if pattern.search(text):
                    found.append((rel, label, label))
    return found, errors


def find_violations(root=ROOT):
    """Return a sorted list of problems, empty when every off site is allowed."""
    found, errors = _scan(root)
    problems = list(errors)
    for rel, where, what in found:
        if (rel, where) not in ALLOWED:
            problems.append(f"{rel}: {where} ({what})")
    return sorted(set(problems))


class NoVerificationOff(unittest.TestCase):
    def test_every_off_site_is_allowed(self):
        self.assertEqual(
            find_violations(),
            [],
            "Verification is off at these sites. Turn it on, or add the site "
            "to ALLOWED with the reason it cannot verify.",
        )

    def test_allowlist_entries_are_real_and_explained(self):
        found, _ = _scan(ROOT)
        real = {(rel, where) for rel, where, _ in found}
        for key, reason in ALLOWED.items():
            self.assertIn(key, real, f"{key} is on ALLOWED but no such site exists")
            self.assertTrue(reason.strip(), f"{key} has no reason")


class PlantedCases(unittest.TestCase):
    """The rule fires on each bad form and stays quiet on a good one."""

    def _run(self, files):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("playbooks", "roles", "inventories", "vars"):
                (root / name).mkdir()
            for rel, text in files.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            return find_violations(root)

    def test_literal_false(self):
        out = self._run({"playbooks/p.yml":
            "- hosts: all\n  tasks:\n    - name: call\n      uri:\n        url: https://x\n"
            "        headers: '{{ request_headers }}'\n        validate_certs: false\n"})
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("playbooks/p.yml: [0].tasks.[call].uri.validate_certs"), out[0])

    def test_default_false(self):
        out = self._run({"playbooks/p.yml":
            "- hosts: all\n  tasks:\n    - name: call\n      uri:\n        url: https://x\n"
            "        validate_certs: \"{{ v | default(false) }}\"\n"})
        self.assertEqual(len(out), 1)

    def test_variable_defaulting_false(self):
        out = self._run({
            "playbooks/p.yml":
                "- hosts: all\n  tasks:\n    - name: call\n      uri:\n        url: https://x\n"
                "        validate_certs: \"{{ r_validate_certs }}\"\n",
            "roles/r/defaults/main.yml": "r_validate_certs: false\n"})
        self.assertEqual(len(out), 2)

    def test_group_var_false(self):
        out = self._run({"inventories/example/group_vars/all/main.yml": "x_ssl_verify: false\n"})
        self.assertEqual(len(out), 1)

    def test_skip_switch_true(self):
        out = self._run({"roles/r/templates/x.yml": "tls_config:\n  insecure_skip_verify: true\n"})
        self.assertEqual(len(out), 1)

    def test_cli_forms(self):
        out = self._run({"roles/r/tasks/main.yml":
            "- name: copy\n  ansible.builtin.command: skopeo copy --dest-tls-verify=false a b\n"
            "- name: fetch\n  ansible.builtin.shell: curl -sk https://x\n"})
        self.assertTrue(any("tls-verify=false" in p for p in out))
        self.assertTrue(any("curl -k" in p for p in out))

    def test_skip_verify_in_a_template(self):
        out = self._run({"roles/r/templates/scrape.yml.j2": "tls_config:\n  insecure_skip_verify: true\n"})
        self.assertEqual(len(out), 1)

    def test_unparseable_file_fails_closed(self):
        out = self._run({"playbooks/p.yml": "- name: [unclosed\n"})
        self.assertEqual(len(out), 1)
        self.assertIn("not parseable", out[0])

    def test_second_switch_next_to_an_allowed_one_fails(self):
        # The allowed blackbox module stays allowed; a second module does not.
        out = self._run({"roles/base/blackbox-exporter/defaults/main.yml":
            "blackbox_modules:\n  http_2xx_insecure:\n    http:\n      tls_config:\n"
            "        insecure_skip_verify: true\n  http_2xx_other:\n    http:\n      tls_config:\n"
            "        insecure_skip_verify: true\n"})
        self.assertEqual(len(out), 1)
        self.assertIn("http_2xx_other", out[0])

    def test_extra_off_key_inside_an_allowed_task_fails(self):
        out = self._run({"roles/shared/gate-ingress-ready/tasks/main.yml":
            "- name: \"Gate: assert Traefik ingress route is live\"\n"
            "  ansible.builtin.uri:\n    url: https://x\n"
            "    validate_certs: \"{{ gate_ingress_validate_certs | default(false) | bool }}\"\n"
            "    ca_verify:\n      tls_verify: false\n"})
        self.assertEqual(len(out), 1)
        self.assertIn("tls_verify", out[0])

    def test_verified_call_passes(self):
        out = self._run({"playbooks/p.yml":
            "- hosts: all\n  tasks:\n    - name: call\n      uri:\n        url: https://x\n"
            "        validate_certs: true\n"})
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
