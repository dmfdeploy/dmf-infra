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

    def test_legacy_rbac_cleanup_follows_container_group_readback(self):
        """Old code could revoke NMOS access before AWX accepted the move."""
        source = (ROLE / "tasks" / "main.yml").read_text()

        cleanup = source.index("Remove legacy AWX controller pod-manager binding from NMOS")
        readback = source.index("Read back DMF catalog Container Group")
        assertion = source.index("Assert DMF catalog Container Group EE worker resource requests are applied")
        self.assertGreater(cleanup, readback)
        self.assertGreater(cleanup, assertion)

    def test_legacy_function_bindings_are_not_retargeted_in_place(self):
        """Parallel grants preserve RBAC for already-running legacy EE pods."""
        source = (ROLE / "tasks" / "main.yml").read_text()

        nmos_binding = source.index(
            "- name: Ensure NMOS AWX launcher RoleBinding exists"
        )
        nmos_binding_end = source.index(
            "# Keep the legacy nmos-cpp-launcher RoleBinding", nmos_binding
        )
        mxl_binding = source.index("- name: Bind catalog launcher SA into mxl")
        mxl_binding_end = source.index(
            "# Keep the existing mxl-launcher RoleBinding", mxl_binding
        )

        self.assertIn("name: dmf-catalog-launcher", source[nmos_binding:nmos_binding_end])
        self.assertIn("name: dmf-catalog-launcher", source[mxl_binding:mxl_binding_end])
        self.assertIn("Assert legacy catalog EE pods have drained from NMOS", source)
        self.assertIn("name: nmos-cpp-launcher\n    namespace: nmos", source)
        self.assertIn("name: mxl-launcher\n    namespace: mxl", source)


if __name__ == "__main__":
    unittest.main()
