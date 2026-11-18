"""Render the console's Helm values template and read the certificate authority keys.

The template passes four inventory variables to the console chart: the network mode,
the approval page name, and the CA fingerprint and expiry. An environment that sets
none of them must render empty strings, so it behaves as it did before the keys existed.
The template renders with Ansible's own templar and no task runs.

Run these tests with Ansible's Python, because they import `ansible`.
"""
import unittest
from pathlib import Path

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "roles/stack/operator/cms/templates/values.yml.j2"
)

# The variables the template needs apart from the four under test.
BASE_VARIABLES = {
    "authentik_host": "auth.example.invalid",
    "cms_authentik_enrollment_flow": "flow",
    "cms_authentik_invitation_ttl_hours": 24,
    "cms_awx_autoscale_enabled": False,
    "cms_awx_autoscale_helper_url": "http://helper.invalid",
    "cms_awx_autoscale_max_startup_wait": 1260,
    "cms_awx_ssl_verify": False,
    "cms_catalog_enabled": False,
    "cms_dev_login_enabled": False,
    "cms_exposure": "host",
    "cms_host": "console.example.invalid",
    "cms_image_pull_policy": "IfNotPresent",
    "cms_image_repository": "registry.example.invalid/dmf-cms",
    "cms_image_tag": "0.0.0",
    "cms_ingress_class": "traefik",
    "cms_media_group_tenant_map": "",
    "cms_media_tenancy": "",
    "cms_mxl_endpoints": "",
    "cms_netbox_ssl_verify": False,
    "cms_oidc_application_slug": "dmf-console",
    "cms_oidc_enabled": True,
    "cms_oidc_issuer_url": "https://auth.example.invalid/application/o/dmf-console/",
    "cms_runtime_mode": "cluster",
    "cms_runtime_secret_authentik_token_key": "k",
    "cms_runtime_secret_awx_autoscale_bearer_key": "k",
    "cms_runtime_secret_awx_token_key": "k",
    "cms_runtime_secret_forgejo_token_key": "k",
    "cms_runtime_secret_name": "dmf-cms-runtime",
    "cms_runtime_secret_netbox_token_key": "k",
    "cms_runtime_secret_oidc_key": "k",
    "cms_runtime_secret_session_key": "k",
    "_cms_runtime_secret_data_merged": {"secretKey": "x"},
    "_cms_catalog_entries": [],
    "dmf_cms_oidc_provider_credentials_parsed": {"client_id": "dmf-cms"},
}

TRUST_VARIABLES = {
    "dmf_network_mode": "public",
    "dmf_approval_page_name": "trust.example.invalid",
    "dmf_local_ca_fingerprint_sha256": "AA:BB:CC:DD",
    "dmf_local_ca_not_after": "2036-01-01T00:00:00Z",
}

EXPECTED_KEYS = {
    "networkMode": "public",
    "approvalPageName": "trust.example.invalid",
    "caFingerprintSha256": "AA:BB:CC:DD",
    "caNotAfter": "2036-01-01T00:00:00Z",
}


def render_env(extra):
    variables = {**BASE_VARIABLES, **extra}
    text = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(TEMPLATE.read_text())
    )
    return yaml.safe_load(text)["env"]


class CmsValuesTrustInfoTest(unittest.TestCase):
    def test_variables_reach_the_env_keys(self):
        env = render_env(TRUST_VARIABLES)
        for key, value in EXPECTED_KEYS.items():
            self.assertEqual(env[key], value, key)

    def test_each_key_reads_its_own_variable(self):
        # Setting one variable must move one key and leave the other three empty.
        for variable, key in (
            ("dmf_network_mode", "networkMode"),
            ("dmf_approval_page_name", "approvalPageName"),
            ("dmf_local_ca_fingerprint_sha256", "caFingerprintSha256"),
            ("dmf_local_ca_not_after", "caNotAfter"),
        ):
            env = render_env({variable: TRUST_VARIABLES[variable]})
            for other in EXPECTED_KEYS:
                expected = TRUST_VARIABLES[variable] if other == key else ""
                self.assertEqual(env[other], expected, f"{variable} -> {other}")

    def test_no_variables_render_empty_strings(self):
        env = render_env({})
        for key in EXPECTED_KEYS:
            self.assertEqual(env[key], "", key)

    def test_existing_env_keys_are_unchanged(self):
        env = render_env({})
        self.assertEqual(env["runtimeMode"], "cluster")
        self.assertEqual(env["devLoginEnabled"], False)
        self.assertEqual(env["secretKeySecret"]["existingSecretName"], "dmf-cms-runtime")


if __name__ == "__main__":
    unittest.main()
