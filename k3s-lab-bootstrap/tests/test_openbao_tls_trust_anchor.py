"""OpenBao's certificate comes from a CA, and clients trust the CA.

If the certificate is its own trust anchor, every renewal changes what clients
must trust and breaks them until they restart. These checks keep the leaf signed
by a CA Issuer and keep every consumer on ca.crt."""
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CERT_TASKS = ROOT / "roles/base/cert-manager/tasks/openbao-certificate.yml"
CERT_DEFAULTS = ROOT / "roles/base/cert-manager/defaults/main.yml"
SCAN_SUFFIXES = {".yml", ".yaml", ".j2", ".sh", ".hcl", ".json"}
SKIPPED_DIRS = {"tests", "charts", "docs", "__pycache__"}
# The cert-manager role's tasks handle the leaf Secret and the CA Secret on purpose.
# The playbook tests (test-openbao-ca-bundle.yml, test-openbao-cert-issuance.yml)
# check what they trust.
BACKSTOP_SKIPS = {"roles/base/cert-manager/tasks"}
# Reading the leaf Secret's certificate key as a value, other than to check that
# it exists or to compare it.
LEAF_READ = re.compile(r"^(?!.*(==|!=)).*data\[.tls\.crt.\](?!\s+is defined)")
# A trust setting that names the leaf file on its own line.
LEAF_AS_TRUST = re.compile(
    r"(caBundle|ca_bundle|ca_b64|CACERT|leader_ca_cert_file|ca_cert_file|cacert)[^\n]*tls\.crt",
    re.I,
)


def default(name):
    return yaml.safe_load(CERT_DEFAULTS.read_text())[name]


def resolve(value):
    """Replace a bare {{ variable }} with its default in the cert-manager role."""
    match = re.fullmatch(r"\{\{\s*(\w+)\s*\}\}", value)
    return default(match.group(1)) if match else value


def definitions():
    found = {}
    for task in yaml.safe_load(CERT_TASKS.read_text()):
        definition = task.get("kubernetes.core.k8s", {}).get("definition")
        if definition:
            found[(definition["kind"], resolve(definition["metadata"]["name"]))] = definition
    return found


class LeafIssuer(unittest.TestCase):
    def test_leaf_is_issued_by_a_ca_issuer(self):
        leaf = definitions()[("Certificate", default("cert_manager_openbao_certificate_name"))]
        issuer = definitions()[("Issuer", resolve(leaf["spec"]["issuerRef"]["name"]))]
        self.assertIn("ca", issuer["spec"])
        self.assertNotIn("selfSigned", issuer["spec"])

    def test_ca_certificate_keeps_its_key_on_renewal(self):
        ca = definitions()[("Certificate", default("cert_manager_openbao_ca_certificate_name"))]
        self.assertTrue(ca["spec"]["isCA"])
        self.assertEqual(ca["spec"]["privateKey"]["rotationPolicy"], "Never")


def read(relative_path):
    return (ROOT / relative_path).read_text()


def tasks(relative_path):
    return yaml.safe_load(read(relative_path))


def task_named(relative_path, module, **wanted):
    """The one task that uses the module with these arguments."""
    found = [
        task
        for task in tasks(relative_path)
        if module in task and all(task[module].get(k) == v for k, v in wanted.items())
    ]
    assert len(found) == 1, f"{relative_path}: {len(found)} tasks use {module} with {wanted}"
    return found[0]


def set_fact_value(relative_path, fact):
    """The value a set_fact task gives to a fact."""
    for task in tasks(relative_path):
        value = task.get("ansible.builtin.set_fact", {}).get(fact)
        if value is not None:
            return value
    raise AssertionError(f"{relative_path} sets no {fact}")


def default_of_role(relative_path, name):
    return yaml.safe_load(read(relative_path))[name]


def paths_after(relative_path, pattern):
    """Every file path that the pattern captures, so none can be skipped."""
    found = re.findall(pattern, read(relative_path))
    assert found, f"{relative_path} has no match for {pattern}"
    return found


class ConsumersTrustTheCa(unittest.TestCase):
    def assert_ca(self, names):
        for name in names:
            self.assertTrue(name.endswith("/ca.crt"), name)

    def assert_reads_the_shared_ca_bundle(self, relative_path, fact, uses):
        include = task_named(relative_path, "ansible.builtin.include_role", name="base/cert-manager")
        self.assertEqual(include["ansible.builtin.include_role"]["tasks_from"], "openbao-ca-bundle.yml")
        # A different Secret name here would make the consumer read the leaf.
        self.assertFalse(
            [k for k in include.get("vars", {}) if "secret_name" in k or "ca_" in k and k != "cert_manager_openbao_namespace"]
        )
        self.assertIn(uses, set_fact_value(relative_path, fact))

    def test_eso_store_reads_the_shared_ca_bundle(self):
        self.assert_reads_the_shared_ca_bundle(
            "roles/base/external-secrets/tasks/openbao-trust.yml",
            "eso_openbao_ca_bundle",
            "cert_manager_openbao_ca_bundle_pem",
        )

    def test_object_storage_stores_read_the_shared_ca_bundle(self):
        self.assert_reads_the_shared_ca_bundle(
            "roles/base/object-storage-credentials/tasks/cluster-secret-stores.yml",
            "_os_ca_b64",
            "cert_manager_openbao_ca_bundle_pem",
        )

    def test_the_shared_ca_bundle_is_the_ca_secrets_certificate(self):
        path = "roles/base/cert-manager/tasks/openbao-ca-bundle.yml"
        secret = task_named(path, "kubernetes.core.k8s_info", kind="Secret")
        self.assertEqual(
            secret["kubernetes.core.k8s_info"]["name"], "{{ cert_manager_openbao_ca_secret_name }}"
        )
        value = set_fact_value(path, "cert_manager_openbao_ca_bundle_pem")
        self.assertIn("['tls.crt']", value)
        self.assertIn(secret["register"], value)

    def test_the_workload_waits_for_the_issuer_the_cert_manager_role_names(self):
        self.assertEqual(
            default_of_role("roles/stack/operator/openbao/defaults/main.yml", "openbao_tls_issuer_name"),
            default("cert_manager_openbao_ca_issuer_name"),
        )

    def test_the_init_status_read_never_defaults_to_not_initialized(self):
        for path in (
            "roles/stack/operator/openbao/tasks/main.yml",
            "roles/stack/operator/openbao/tasks/read-init-status.yml",
        ):
            self.assertNotRegex(read(path), r"openbao_is_initialized:\s*false")
        self.assertIn("read-init-status.yml", read("roles/stack/operator/openbao/tasks/main.yml"))

    def test_the_proof_does_not_pin_the_leaf_ca_crt_to_the_current_ca(self):
        # A renewed root has the same key and subject, so the proof verifies the
        # leaf against it. A byte comparison would block every run until the leaf
        # is reissued.
        task = next(
            t
            for t in tasks("roles/base/cert-manager/tasks/openbao-issuance-proof.yml")[0]["block"]
            if t.get("register") == "cert_manager_openbao_leaf_secret_read"
        )
        self.assertEqual([c for c in task["until"] if "ca.crt" in c], [])

    def test_the_certificate_file_proves_the_issuance_before_the_reload(self):
        included = [
            task["ansible.builtin.include_tasks"]
            for task in tasks("roles/base/cert-manager/tasks/openbao-certificate.yml")
            if "ansible.builtin.include_tasks" in task
        ]
        names = [i if isinstance(i, str) else i["file"] for i in included]
        self.assertEqual(
            names[-3:],
            ["openbao-wait-certificate.yml", "openbao-issuance-proof.yml", "openbao-listener-reload.yml"],
        )

    def test_statefulset_sets_the_cli_trust_to_the_ca(self):
        self.assert_ca(
            paths_after(
                "roles/stack/operator/openbao/templates/statefulset.yml.j2",
                r'name: BAO_CACERT\s+value: "([^"]+)"',
            )
        )

    def test_raft_retry_join_trusts_the_ca(self):
        self.assert_ca(
            paths_after(
                "roles/stack/operator/openbao/templates/config.hcl.j2",
                r'leader_ca_cert_file\s*=\s*"([^"]+)"',
            )
        )

    def test_in_pod_cli_calls_trust_the_ca(self):
        for relative_path in (
            "playbooks/runbooks/eso-openbao-health-check.yml",
            "playbooks/verify-bootstrap-convergence.yml",
        ):
            self.assert_ca(paths_after(relative_path, r"BAO_CACERT=([^\s;\"']+)"))

    def test_no_other_file_uses_the_leaf_as_trust_anchor(self):
        # A backstop for a consumer that the checks above do not name.
        offenders = []
        for path in sorted(ROOT.rglob("*")):
            parts = path.relative_to(ROOT).parts
            if not path.is_file() or path.suffix not in SCAN_SUFFIXES or SKIPPED_DIRS & set(parts):
                continue
            if any(path.relative_to(ROOT).as_posix().startswith(skip) for skip in BACKSTOP_SKIPS):
                continue
            for number, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
                if LEAF_AS_TRUST.search(line) or LEAF_READ.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
