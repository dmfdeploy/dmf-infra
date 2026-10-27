"""Static regression checks for the Forgejo Authentik access policy.

These checks deliberately inspect the rendered-input templates and role
defaults without contacting Authentik, Forgejo, Kubernetes, or a secret store.
"""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
AUTHENTIK = ROOT / "roles/stack/operator/authentik"
FORGEJO = ROOT / "roles/stack/operator/forgejo"


class ForgejoContributorPolicyTests(unittest.TestCase):
    def test_authentik_subject_is_stable_uuid(self):
        providers = (AUTHENTIK / "templates/blueprints/20-app-providers.yaml.j2").read_text()
        forgejo = providers.split("# ── Forgejo ─", 1)[1].split("# ── AWX ─", 1)[0]
        self.assertIn("sub_mode: user_uuid", forgejo)
        self.assertNotIn("sub_mode: user_username", forgejo)

    def test_app_has_only_the_two_explicit_group_bindings(self):
        groups = (AUTHENTIK / "files/blueprints/10-baseline-groups.yaml").read_text()
        providers = (AUTHENTIK / "templates/blueprints/20-app-providers.yaml.j2").read_text()
        app = providers.split("id: app-forgejo", 1)[1].split("# ── AWX ─", 1)[0]

        self.assertIn("name: forgejo-contributor", groups)
        self.assertIn("is_superuser: false", groups.split("name: forgejo-contributor", 1)[1])
        self.assertEqual(app.count("model: authentik_policies.policybinding"), 2)
        self.assertIn("group: !Find [authentik_core.group, [name, forgejo-contributor]]", app)
        self.assertIn("group: !Find [authentik_core.group, [name, ops-admin]]", app)
        self.assertIn("policy_engine_mode: any", app)

    def test_forgejo_signup_and_linking_policy(self):
        defaults = (FORGEJO / "defaults/main.yml").read_text()
        values = (FORGEJO / "templates/values.yml.j2").read_text()

        self.assertIn("forgejo_oauth_account_linking: disabled", defaults)
        self.assertIn("forgejo_oauth_username_source: userid", defaults)
        self.assertIn("forgejo_oauth_enable_auto_registration: true", defaults)
        self.assertIn("forgejo_disable_local_registration: true", defaults)
        self.assertIn("forgejo_enable_openid_signup: false", defaults)
        self.assertIn("DISABLE_REGISTRATION:", values)
        self.assertIn("ENABLE_OPENID_SIGNUP:", values)
        self.assertIn("ACCOUNT_LINKING:", values)
        self.assertIn("USERNAME:", values)
        self.assertIn("ENABLE_AUTO_REGISTRATION:", values)


if __name__ == "__main__":
    unittest.main()
