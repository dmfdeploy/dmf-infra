"""Regression guards for dmfdeploy/dmfdeploy#748."""

import pathlib
import unittest


ROLE = pathlib.Path(__file__).resolve().parent.parent


class ProtectedLauncherNamespaceTests(unittest.TestCase):
    def test_catalog_pods_use_the_protected_identity(self):
        """The old override ran catalog EE pods as the nmos launcher SA."""
        defaults = (ROLE / "defaults" / "main.yml").read_text()
        source = (ROLE / "tasks" / "main.yml").read_text()

        self.assertIn("awx_dmf_catalog_namespace: dmf-catalog", defaults)
        self.assertIn("awx_dmf_catalog_service_account: dmf-catalog-launcher", defaults)
        self.assertIn('namespace: "{{ awx_dmf_catalog_namespace }}"', source)
        self.assertIn('serviceAccountName: "{{ awx_dmf_catalog_service_account }}"', source)
        self.assertNotIn("serviceAccountName: nmos-cpp-launcher", source)

    def test_function_namespace_bindings_reference_only_protected_subject(self):
        """Function namespaces must not host a launcher ServiceAccount."""
        source = (ROLE / "tasks" / "main.yml").read_text()

        self.assertIn('name: "{{ awx_dmf_catalog_service_account }}"', source)
        self.assertIn('namespace: "{{ awx_dmf_catalog_namespace }}"', source)
        self.assertNotIn("Ensure NMOS AWX launcher service account exists", source)
        self.assertIn("Remove legacy AWX controller pod-manager binding from NMOS", source)


if __name__ == "__main__":
    unittest.main()
