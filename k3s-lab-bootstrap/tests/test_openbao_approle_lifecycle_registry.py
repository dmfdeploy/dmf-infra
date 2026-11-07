"""Completeness checks for the finite-TTL OpenBao AppRole registry."""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "vars/openbao-approle-lifecycle-registry.json"
OPENBAO_DEFAULTS = ROOT / "roles/stack/operator/openbao/defaults/main.yml"
OBJECT_STORAGE_DEFAULTS = ROOT / "roles/base/object-storage-credentials/defaults/main.yml"
ESO_DEFAULTS = ROOT / "roles/base/external-secrets/defaults/main.yml"
LONGHORN_DEFAULTS = ROOT / "roles/base/longhorn-backup-target/defaults/main.yml"
OPENBAO_TASKS = ROOT / "roles/stack/operator/openbao/tasks/main.yml"
OBJECT_STORAGE_TASKS = ROOT / "roles/base/object-storage-credentials/tasks/main.yml"


def scalar(text, name):
    match = re.search(rf"^{re.escape(name)}:\s*['\"]?([^\s'\"]+)", text, re.M)
    if not match:
        raise AssertionError(f"missing configured value {name}")
    return match.group(1)


def object_storage_logicals(text):
    match = re.search(
        r"^object_storage_logicals:\s*\n((?:\s+-\s+[^\n]+\n)+)", text, re.M
    )
    if not match:
        raise AssertionError("missing object_storage_logicals")
    return [value.strip() for value in re.findall(r"^\s+-\s+([^\s#]+)", match.group(1), re.M)]


REPO = ROOT.parent
REGISTRY_RELATIVE = "k3s-lab-bootstrap/vars/openbao-approle-lifecycle-registry.json"
# The rule has two parts.
#
# 1. Every file in the repository whose text mentions a SecretID TTL (in any
#    spelling or case) or an AppRole role path must be the registry, a file
#    that the registry names as a create site, or an entry in EXCLUDED. A new
#    file that mentions either fails until someone reads it and either
#    registers it or excludes it with a reason.
# 2. Inside a registered or excluded file, the number of such mentions is
#    pinned in PINNED_MATCHES. A new declaration in an allowed file changes
#    the count and fails. Register the new declaration in the registry, then
#    update the pin.
CANDIDATE = re.compile(r"secret[_-]?id[_-]?ttl|auth/approle/role/", re.I)
EXCLUDED = {
    "k3s-lab-bootstrap/docs/openbao-approle-lifecycle-registry.md": "documents the registry and quotes its field names",
    "k3s-lab-bootstrap/lib/approle_lifecycle/registry.py": "reads the registry fields and creates no role",
    "k3s-lab-bootstrap/lib/approle_lifecycle/setup_only.py": "reads the create response of a per-run SecretID for a role created elsewhere",
    "k3s-lab-bootstrap/playbooks/verify-openbao-identity-model.yml": "reads SecretIDs of an existing role to verify identities and creates no role",
    "k3s-lab-bootstrap/playbooks/vertical-orchestration/110-eso-secret-rotation.yml": "mints a SecretID for an existing role, which inherits the role TTL",
    "k3s-lab-bootstrap/roles/base/prometheus/files/rules/recording.yaml": "uses the metric name dmf_openbao_approle_secret_id_ttl_seconds",
    "k3s-lab-bootstrap/roles/base/prometheus/templates/rules/alerts.yaml.j2": "uses the metric name dmf_openbao_approle_secret_id_ttl_seconds",
    "k3s-lab-bootstrap/roles/base/prometheus/tests/test_openbao_approle_rotation_396.promtool.yml": "feeds the metric name to promtool",
    "k3s-lab-bootstrap/tests/openbao_fakes.py": "in-memory client that mimics the create response",
    "k3s-lab-bootstrap/tests/test_openbao_approle_lifecycle_registry.py": "this check, which quotes the patterns",
    "k3s-lab-bootstrap/tests/test_openbao_approle_registry_assertions.py": "tests the role assertions that name the removed overrides",
    "k3s-lab-bootstrap/tests/test_openbao_approle_registry_model.py": "tests the registry model with TTL values",
    "k3s-lab-bootstrap/tests/test_openbao_setup_only_issuance.py": "tests the setup-only path against the create response",
}


PINNED_MATCHES = {
    "k3s-lab-bootstrap/docs/openbao-approle-lifecycle-registry.md": 7,
    "k3s-lab-bootstrap/lib/approle_lifecycle/registry.py": 3,
    "k3s-lab-bootstrap/lib/approle_lifecycle/setup_only.py": 4,
    "k3s-lab-bootstrap/playbooks/verify-openbao-identity-model.yml": 2,
    "k3s-lab-bootstrap/playbooks/vertical-orchestration/110-eso-secret-rotation.yml": 1,
    "k3s-lab-bootstrap/roles/base/object-storage-credentials/tasks/main.yml": 9,
    "k3s-lab-bootstrap/roles/base/prometheus/files/rules/recording.yaml": 1,
    "k3s-lab-bootstrap/roles/base/prometheus/templates/rules/alerts.yaml.j2": 3,
    "k3s-lab-bootstrap/roles/base/prometheus/tests/test_openbao_approle_rotation_396.promtool.yml": 3,
    "k3s-lab-bootstrap/roles/stack/operator/openbao/tasks/main.yml": 19,
    "k3s-lab-bootstrap/tests/openbao_fakes.py": 1,
    "k3s-lab-bootstrap/tests/test_openbao_approle_lifecycle_registry.py": 31,
    "k3s-lab-bootstrap/tests/test_openbao_approle_registry_assertions.py": 4,
    "k3s-lab-bootstrap/tests/test_openbao_approle_registry_model.py": 1,
    "k3s-lab-bootstrap/tests/test_openbao_setup_only_issuance.py": 13,
}


def repository_files(repo=REPO):
    """Every text file in the repository that mentions a TTL or an AppRole role
    path, keyed by its path relative to the repository root.

    The list comes from `git ls-files` with no directory pruning: tracked files
    plus untracked files that git does not ignore, so a new file is checked
    before it is committed. Files that git ignores are out of scope because CI
    cannot see them."""
    listing = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        check=True,
    ).stdout
    found = {}
    for relative in sorted(name for name in listing.decode("utf-8").split("\0") if name):
        path = Path(repo) / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if CANDIDATE.search(text):
            found[relative] = text
    return found


def create_sites(registry):
    sites = {
        "k3s-lab-bootstrap/" + entry["create_site"]
        for group in ("runtime_targets", "dispositions")
        for entry in registry.get(group, {}).values()
    }
    return sites


def unregistered_files(files, registry):
    """Name every file that mentions a TTL or a role path and is neither the
    registry, a registered create site, nor on the exclusion list."""
    allowed = {REGISTRY_RELATIVE} | create_sites(registry) | set(EXCLUDED)
    return [
        f"{path} mentions a SecretID TTL or an AppRole role path and is neither registered nor excluded"
        for path in sorted(files)
        if path not in allowed
    ]


def pinned_paths(registry):
    return create_sites(registry) | set(EXCLUDED)


def pin_errors(files, registry):
    """Compare the number of mentions in each registered or excluded file with
    its pin."""
    errors = []
    for path in sorted(pinned_paths(registry)):
        if path not in files:
            continue
        actual = len(CANDIDATE.findall(files[path]))
        expected = PINNED_MATCHES.get(path)
        if expected != actual:
            errors.append(
                f"{path} has {actual} mentions of a SecretID TTL or an AppRole role path, "
                f"the pin says {expected}: register the new declaration in the registry, "
                "then update PINNED_MATCHES"
            )
    return errors


def scanned_texts(extra=None):
    """The texts the create-site checks read: every candidate file that is not
    the registry and not excluded, which is the registered create sites plus
    any file the rule above would flag."""
    skipped = {REGISTRY_RELATIVE} | set(EXCLUDED)
    files = repository_files()
    files.update(extra or {})
    return [text for path, text in sorted(files.items()) if path not in skipped]


ROLE_CREATE = re.compile(r"bao\s+write\s+auth/approle/role/([^\s/'\"]+)(?=\s)")


def role_creation_errors(texts):
    """Check the `bao write auth/approle/role/<name>` create commands in the
    given texts: each must set a TTL, a TTL key must not appear outside the
    registry, and there must be exactly three create sites. This check knows
    one create-command spelling. The two-part rule above covers the rest: a
    file that mentions a role path any other way is flagged unless it is
    allowed, and a new mention inside an allowed file changes its pinned
    count."""
    errors = []
    sites = 0
    for text in texts:
        for match in ROLE_CREATE.finditer(text):
            sites += 1
            following = text[match.end():]
            end = re.search(r"\n\s*- name:", following)
            command = following[: end.start()] if end else following
            if "secret_id_ttl=" not in command:
                errors.append(
                    f"AppRole {match.group(1)} is created without a registry TTL: "
                    "add it to the registry or give it an explicit disposition"
                )
        if re.search(r"^\s*secret_id_ttl\s*:", text, re.M) or re.search(r"secret[-_]id[-_]ttl\s*=\s*[^{\s]", text):
            errors.append("a SecretID TTL is set outside the registry")
    if sites != 3:
        errors.append(f"expected exactly three AppRole create sites, found {sites}")
    return errors


def ttl_assignment_inventory(*texts):
    expressions = set()
    count = 0
    for text in texts:
        assignments = re.findall(
            r"\bsecret_id_ttl\s*=\s*(\{\{[^}]+\}\})", text
        )
        occurrences = re.findall(r"\bsecret_id_ttl\s*=", text)
        if len(assignments) != len(occurrences):
            raise AssertionError("every AppRole TTL assignment must use a registry expression")
        expressions.update(assignments)
        count += len(occurrences)
    return expressions, count


def current_code_inventory():
    openbao_defaults = OPENBAO_DEFAULTS.read_text(encoding="utf-8")
    object_defaults = OBJECT_STORAGE_DEFAULTS.read_text(encoding="utf-8")
    eso_defaults = ESO_DEFAULTS.read_text(encoding="utf-8")
    longhorn_defaults = LONGHORN_DEFAULTS.read_text(encoding="utf-8")
    openbao_tasks = OPENBAO_TASKS.read_text(encoding="utf-8")
    object_tasks = OBJECT_STORAGE_TASKS.read_text(encoding="utf-8")

    runtime = {
        scalar(openbao_defaults, "openbao_eso_role_name"),
        *(
            "object-storage-" + logical.replace("_", "-")
            for logical in object_storage_logicals(object_defaults)
        ),
    }
    setup_only = {scalar(openbao_defaults, "openbao_born_inventory_role_name")}
    task_texts = scanned_texts()
    if any(
        re.search(r"^\s*secret_id_ttl:\s*['\"]?[0-9]+[smhdw]", text, re.M)
        for text in task_texts
    ):
        raise AssertionError("literal finite SecretID TTLs must live in the registry")
    ttl_expressions, ttl_assignment_count = ttl_assignment_inventory(*task_texts)
    return runtime, setup_only, ttl_expressions, ttl_assignment_count


def registry_coverage_errors(
    registry, runtime, setup_only, ttl_expressions, ttl_assignment_count
):
    registry_runtime = set(registry.get("runtime_targets", {}))
    registry_setup = set(registry.get("dispositions", {}))
    errors = []
    if registry_runtime != runtime:
        errors.append(
            f"runtime targets differ: missing={sorted(runtime - registry_runtime)}, "
            f"extra={sorted(registry_runtime - runtime)}"
        )
    if registry_setup != setup_only:
        errors.append(
            f"setup dispositions differ: missing={sorted(setup_only - registry_setup)}, "
            f"extra={sorted(registry_setup - setup_only)}"
        )
    if registry_runtime & registry_setup:
        errors.append("a role appears as both runtime and setup-only")

    expected_ttl_expressions = {
        "{{openbao_approle_lifecycle_registry.runtime_targets['external-secrets'].secret_id_ttl}}",
        "{{openbao_approle_lifecycle_registry.runtime_targets['object-storage-' ~ item.logical_item.logical_dashed].secret_id_ttl}}",
        "{{openbao_approle_lifecycle_registry.dispositions['born-inventory-netbox-reader'].secret_id_ttl}}",
    }
    if ttl_expressions != expected_ttl_expressions:
        errors.append(
            f"TTL write sites must read registry values: expressions={sorted(ttl_expressions)}"
        )
    if ttl_assignment_count != 3:
        errors.append(
            f"expected exactly three finite-TTL role write sites, found {ttl_assignment_count}"
        )

    for target, entry in registry.get("runtime_targets", {}).items():
        if entry.get("approle_name") != target:
            errors.append(f"{target}: target and AppRole name differ")
        if not re.fullmatch(r"[1-9][0-9]*h", entry.get("secret_id_ttl", "")):
            errors.append(f"{target}: finite SecretID TTL is missing or malformed")
        if not entry.get("destinations") or not entry.get("readiness_gates"):
            errors.append(f"{target}: destination/readiness evidence is missing")
        if any(gate.get("status") == "required_rotation_gate" and not gate.get("evidence_gap")
               for gate in entry.get("readiness_gates", [])):
            errors.append(f"{target}: a new readiness gate is claimed without current evidence")
    for target, entry in registry.get("dispositions", {}).items():
        if entry.get("approle_name") != target:
            errors.append(f"{target}: disposition and AppRole name differ")
        if not re.fullmatch(r"[1-9][0-9]*h", entry.get("secret_id_ttl", "")):
            errors.append(f"{target}: finite SecretID TTL is missing or malformed")
        if entry.get("disposition") != "setup_only" or entry.get("continuous_rotation") is not False:
            errors.append(f"{target}: setup-only exclusion is not explicit")
    return errors


class OpenBaoAppRoleLifecycleRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        cls.inventory = current_code_inventory()

    def test_registry_covers_each_finite_ttl_role_exactly_once(self):
        self.assertEqual(self.registry["schema_version"], 1)
        self.assertEqual(
            registry_coverage_errors(self.registry, *self.inventory), []
        )
        for relative_role, levels_up in (
            ("roles/stack/operator/openbao/tasks/main.yml", "../../../../"),
            ("roles/base/object-storage-credentials/tasks/main.yml", "../../../"),
        ):
            task_text = (ROOT / relative_role).read_text(encoding="utf-8")
            self.assertIn(
                f'file: "{{{{ role_path }}}}/{levels_up}vars/openbao-approle-lifecycle-registry.json"',
                task_text,
            )
            self.assertIn("name: openbao_approle_lifecycle_registry", task_text)

    def test_an_added_runtime_role_fails_closed_until_registered(self):
        runtime, setup_only, ttl_expressions, ttl_count = self.inventory
        errors = registry_coverage_errors(
            self.registry,
            runtime | {"new-finite-role"},
            setup_only,
            ttl_expressions,
            ttl_count + 1,
        )
        self.assertTrue(any("new-finite-role" in error for error in errors))

    def test_an_added_ttl_assignment_is_detected_even_if_it_reuses_a_variable(self):
        runtime, setup_only, ttl_expressions, ttl_count = self.inventory
        errors = registry_coverage_errors(
            self.registry, runtime, setup_only, ttl_expressions, ttl_count + 1
        )
        self.assertTrue(any("write sites" in error for error in errors))

    def test_every_role_create_command_in_the_tree_sets_a_registry_ttl(self):
        self.assertEqual(role_creation_errors(scanned_texts()), [])

    def test_an_unregistered_finite_ttl_role_fixture_fails_the_check(self):
        rogue = (
            "- name: Create a rogue role\n"
            "  ansible.builtin.shell:\n"
            "    cmd: >-\n"
            "      bao write auth/approle/role/rogue-role token_policies=x secret_id_ttl=24h\n"
        )
        errors = role_creation_errors([*scanned_texts(), rogue])
        self.assertTrue(any("three AppRole create sites" in error for error in errors), errors)
        self.assertTrue(any("outside the registry" in error for error in errors), errors)
        with self.assertRaises(AssertionError):
            ttl_assignment_inventory(*scanned_texts(), rogue)

    def test_a_role_created_without_any_ttl_fixture_fails_the_check(self):
        rogue = (
            "- name: Create a role with no TTL\n"
            "  ansible.builtin.shell:\n"
            "    cmd: bao write auth/approle/role/no-ttl token_policies=x\n"
        )
        errors = role_creation_errors([rogue])
        self.assertTrue(any("no-ttl is created without a registry TTL" in error for error in errors), errors)

    def test_a_ttl_key_outside_the_registry_fixture_fails_the_check(self):
        keyed = "vars:\n  secret_id_ttl: \"{{ some_variable }}\"\n"
        errors = role_creation_errors([keyed])
        self.assertTrue(any("outside the registry" in error for error in errors), errors)

    def test_every_registered_or_excluded_file_has_a_pin_and_it_matches(self):
        self.assertEqual(set(PINNED_MATCHES), pinned_paths(self.registry))
        self.assertEqual(pin_errors(repository_files(), self.registry), [])

    def test_a_json_post_appended_to_a_registered_create_site_fails_the_pin(self):
        site = "k3s-lab-bootstrap/roles/stack/operator/openbao/tasks/main.yml"
        rogue = "\nrequests.post(base + '/v1/auth/approle/role/rogue', json={'secret_id_ttl': '24h'})\n"
        files = repository_files()
        files[site] = files[site] + rogue
        errors = pin_errors(files, self.registry)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn(site, errors[0])
        self.assertIn("update PINNED_MATCHES", errors[0])
        # The file rule alone and the create-command check both miss it, which is why the pin exists.
        self.assertEqual(unregistered_files(files, self.registry), [])
        self.assertEqual(role_creation_errors(scanned_texts({site: files[site]})), [])

    def test_a_new_mention_in_an_excluded_file_fails_the_pin(self):
        excluded = "k3s-lab-bootstrap/lib/approle_lifecycle/registry.py"
        files = repository_files()
        files[excluded] = files[excluded] + "\n# secret_id_ttl\n"
        self.assertEqual(len(pin_errors(files, self.registry)), 1)

    def test_a_tracked_file_under_node_modules_or_pycache_is_scanned(self):
        rogue = "TTL = 'secret_id_ttl'\nPATH = '/v1/auth/approle/role/rogue'\n"
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "init", "-q", directory], check=True)
            for relative in ("node_modules/myrole/rogue.py", "pkg/__pycache__/rogue.py"):
                path = Path(directory) / relative
                path.parent.mkdir(parents=True)
                path.write_text(rogue, encoding="utf-8")
            subprocess.run(["git", "-C", directory, "add", "."], check=True)
            files = repository_files(Path(directory))
        self.assertEqual(set(files), {"node_modules/myrole/rogue.py", "pkg/__pycache__/rogue.py"})

    def test_a_git_ignored_file_is_out_of_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "init", "-q", directory], check=True)
            (Path(directory) / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
            (Path(directory) / "ignored.py").write_text("TTL = 'secret_id_ttl'\n", encoding="utf-8")
            self.assertEqual(repository_files(Path(directory)), {})

    def test_every_file_that_mentions_a_ttl_or_a_role_path_is_registered_or_excluded(self):
        self.assertEqual(unregistered_files(repository_files(), self.registry), [])

    def test_the_exclusion_list_has_no_stale_entries(self):
        files = repository_files()
        for path, reason in EXCLUDED.items():
            self.assertTrue(reason.strip(), path)
            self.assertIn(path, files, f"{path} no longer mentions a TTL or a role path; remove it")

    def test_the_registered_create_sites_exist_and_are_found(self):
        files = repository_files()
        for site in create_sites(self.registry):
            self.assertIn(site, files)

    def test_a_json_post_that_creates_a_role_with_a_ttl_fixture_fails_the_rule(self):
        rogue = (
            "import requests\n"
            "requests.post(base + '/v1/auth/approle/role/rogue', json={'secret_id_ttl': '24h'})\n"
        )
        files = {**repository_files(), "k3s-lab-bootstrap/lib/rogue.py": rogue}
        errors = unregistered_files(files, self.registry)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("lib/rogue.py", errors[0])

    def test_other_spellings_and_places_fixture_fail_the_rule(self):
        spellings = {
            "tools/camel.py": "payload = {'secretIdTtl': '24h'}\n",
            "charts/x/templates/job.yaml": "args: ['--secret-id-ttl=24h']\n",
            "scripts/bootstrap.sh": "vault write auth/approle/role/new token_policies=p\n",
            "k3s-lab-bootstrap/lib/approle_lifecycle/extra.py": "TTL = 'secret_id_ttl'\n",
        }
        for path, text in spellings.items():
            with self.subTest(path):
                errors = unregistered_files({**repository_files(), path: text}, self.registry)
                self.assertEqual(len(errors), 1, errors)

    def test_a_file_with_no_mention_is_not_a_candidate(self):
        self.assertFalse(CANDIDATE.search("print('nothing to see')"))
        self.assertEqual(unregistered_files(repository_files(), self.registry), [])

    def test_setup_only_role_has_no_rotation_target_or_monitoring_record(self):
        runtime = set(self.registry["runtime_targets"])
        disposition = self.registry["dispositions"]["born-inventory-netbox-reader"]
        self.assertNotIn("born-inventory-netbox-reader", runtime)
        self.assertFalse(disposition["continuous_rotation"])
        self.assertFalse(disposition["continuous_expiry_monitoring"])

    def test_object_storage_readiness_gap_is_explicit(self):
        for target in (
            "object-storage-openbao-snapshots",
            "object-storage-app-backups",
        ):
            gates = self.registry["runtime_targets"][target]["readiness_gates"]
            deferred = [gate for gate in gates if gate["status"] == "required_rotation_gate"]
            self.assertTrue(deferred, target)
            self.assertTrue(all(gate.get("evidence_gap") for gate in deferred))

    def test_destinations_and_existing_readiness_checks_match_consumer_configuration(self):
        object_defaults = OBJECT_STORAGE_DEFAULTS.read_text(encoding="utf-8")
        eso_defaults = ESO_DEFAULTS.read_text(encoding="utf-8")
        longhorn_defaults = LONGHORN_DEFAULTS.read_text(encoding="utf-8")
        external = self.registry["runtime_targets"]["external-secrets"]
        external_destination = external["destinations"][0]
        self.assertEqual(external_destination["namespace"], scalar(eso_defaults, "eso_namespace"))
        self.assertEqual(external_destination["name"], scalar(eso_defaults, "eso_approle_secret_name"))
        self.assertEqual(external_destination["key"], "id")
        self.assertTrue(any(gate.get("status") == "existing_check"
                            for gate in external["readiness_gates"]))
        self.assertEqual(
            next(gate["name"] for gate in external["readiness_gates"]
                 if gate["kind"] == "ClusterSecretStore"),
            scalar(eso_defaults, "eso_cluster_secret_store_name"),
        )

        logicals = object_storage_logicals(object_defaults)
        for logical in logicals:
            dashed = logical.replace("_", "-")
            target = f"object-storage-{dashed}"
            entry = self.registry["runtime_targets"][target]
            self.assertEqual(entry["logical_name"], logical)
            destinations = entry["destinations"]
            self.assertIn(
                {
                    "kind": "openbao_kv2_record",
                    "path": f"secret/data/platform/eso-bindings/{target}",
                    "key": "secret_id",
                },
                destinations,
            )
            self.assertEqual(
                next(gate["name"] for gate in entry["readiness_gates"]
                     if gate["kind"] == "ClusterSecretStore"),
                f"openbao-{dashed}",
            )
            external_secret_gate = next(
                gate for gate in entry["readiness_gates"]
                if gate["kind"] == "ExternalSecret"
            )
            self.assertEqual(
                external_secret_gate["namespaces_from"],
                f"object_storage_consumer_namespaces.{logical}",
            )
            self.assertEqual(
                external_secret_gate["name_pattern"], f"s3-creds-{dashed}"
            )
            self.assertIn(
                {
                    "kind": "kubernetes_secret_key",
                    "namespace": scalar(object_defaults, "object_storage_eso_namespace"),
                    "name": f"{dashed}-openbao-approle",
                    "key": "id",
                },
                destinations,
            )

        backup_target = self.registry["runtime_targets"]["object-storage-app-backups"]
        longhorn_gate = next(
            gate for gate in backup_target["readiness_gates"]
            if gate["kind"] == "KubernetesSecret"
        )
        self.assertEqual(longhorn_gate["namespace"], scalar(longhorn_defaults, "longhorn_backup_target_namespace"))
        self.assertEqual(longhorn_gate["name"], scalar(longhorn_defaults, "longhorn_backup_target_secret_name"))
        self.assertEqual(longhorn_gate["required_key"], "AWS_ACCESS_KEY_ID")


if __name__ == "__main__":
    unittest.main()
