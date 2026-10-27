"""Rendered-config regression checks for the Forgejo Authentik access policy.

These checks render the configuration and evaluate the declared app bindings
without contacting Authentik, Forgejo, Kubernetes, or a secret store.
"""

from pathlib import Path
import json
import re
import sys
import unittest

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[1]
AUTHENTIK = ROOT / "roles/stack/operator/authentik"
FORGEJO = ROOT / "roles/stack/operator/forgejo"
sys.path.insert(0, str(AUTHENTIK / "scripts"))

from validate_forgejo_oidc_subject_mode import validate_subject_mode  # noqa: E402


class ForgejoContributorPolicyTests(unittest.TestCase):
    def test_default_authentik_subject_preserves_existing_bindings(self):
        defaults = yaml.safe_load((AUTHENTIK / "defaults/main.yml").read_text())
        subject_mode = defaults["authentik_forgejo_oidc_sub_mode"]
        self.assertEqual(subject_mode, "user_username")
        self.assertEqual(validate_subject_mode(subject_mode), "user_username")
        rendered = render_authentik_blueprint(subject_mode)
        blueprint = yaml.load(rendered, Loader=AuthentikBlueprintLoader)
        forgejo_provider = next(entry for entry in blueprint["entries"] if entry.get("id") == "oidc-forgejo")
        self.assertEqual(forgejo_provider["attrs"]["sub_mode"], "user_username")

    def test_uuid_subject_is_rejected_until_supported_migration_exists(self):
        with self.assertRaisesRegex(ValueError, "supported, version-specific migration"):
            validate_subject_mode("user_uuid")

    def test_only_contributor_and_ops_admin_groups_can_reach_app(self):
        groups = yaml.safe_load((AUTHENTIK / "files/blueprints/10-baseline-groups.yaml").read_text())
        contributor_group = next(
            entry for entry in groups["entries"]
            if entry.get("id") == "dmf-group-forgejo-contributor"
        )
        self.assertIs(contributor_group["attrs"]["is_superuser"], False)
        blueprint = yaml.load(render_authentik_blueprint("user_username"), Loader=AuthentikBlueprintLoader)
        app = next(entry for entry in blueprint["entries"] if entry.get("id") == "app-forgejo")
        bindings = [
            entry for entry in blueprint["entries"]
            if entry.get("model") == "authentik_policies.policybinding"
            and entry["attrs"]["target"] == "app-forgejo"
        ]
        bound_groups = {entry["attrs"]["group"][1][1] for entry in bindings}
        self.assertEqual(bound_groups, {"forgejo-contributor", "ops-admin"})
        self.assertEqual(app["attrs"]["policy_engine_mode"], "any")
        self.assertTrue(allowed_by_application({"forgejo-contributor"}, bound_groups))
        self.assertTrue(allowed_by_application({"ops-admin"}, bound_groups))
        self.assertFalse(allowed_by_application({"ordinary-user"}, bound_groups))
        self.assertFalse(allowed_by_application(set(), bound_groups))

    def test_rendered_forgejo_configuration_has_closed_local_signup_and_oauth_only(self):
        rendered = render_forgejo_values()
        values = yaml.safe_load(rendered)
        config = values["gitea"]["config"]
        self.assertIs(config["service"]["DISABLE_REGISTRATION"], True)
        self.assertIs(config["openid"]["ENABLE_OPENID_SIGNUP"], False)
        self.assertIs(config["oauth2_client"]["ENABLE_AUTO_REGISTRATION"], True)
        self.assertEqual(config["oauth2_client"]["ACCOUNT_LINKING"], "disabled")
        self.assertEqual(config["oauth2_client"]["USERNAME"], "userid")
        self.assertEqual(values["gitea"]["oauth"][0]["name"], "Authentik")


class AuthentikBlueprintLoader(yaml.SafeLoader):
    pass


AuthentikBlueprintLoader.add_constructor("!Find", lambda loader, node: loader.construct_sequence(node))
AuthentikBlueprintLoader.add_constructor("!KeyOf", lambda loader, node: loader.construct_scalar(node))


def render_authentik_blueprint(subject_mode):
    template = (AUTHENTIK / "templates/blueprints/20-app-providers.yaml.j2").read_text()
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
    environment.filters["regex_replace"] = lambda value, pattern, replacement: re.sub(pattern, replacement, value)
    environment.filters["to_json"] = json.dumps
    return environment.from_string(template).render(authentik_forgejo_oidc_sub_mode=subject_mode)


def render_forgejo_values():
    defaults = yaml.safe_load((FORGEJO / "defaults/main.yml").read_text())
    defaults.update({
        "forgejo_external_host": "forgejo.dmf.example.com",
        "forgejo_external_scheme": "https",
        "forgejo_external_base_url": "https://forgejo.dmf.example.com",
        "forgejo_root_url": "https://forgejo.dmf.example.com/",
        "forgejo_admin_email": "break-glass@dmf.example.com",
        "forgejo_admin_password": "test-only-password",
        "authentik_host": "auth.dmf.example.com",
        "forgejo_oauth_auto_discover_url": "https://auth.dmf.example.com/application/o/forgejo/.well-known/openid-configuration",
        "forgejo_local_ca_trust_enabled": False,
    })
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
    environment.filters["to_json"] = json.dumps
    environment.filters["to_nice_yaml"] = lambda value, indent=2: yaml.safe_dump(
        value, default_flow_style=False, sort_keys=False, indent=indent
    ).rstrip()
    environment.filters["bool"] = bool
    template = (FORGEJO / "templates/values.yml.j2").read_text()
    return environment.from_string(template).render(**defaults)


def allowed_by_application(user_groups, bound_groups):
    """Authentik's `any` policy mode grants access when a binding matches."""
    return bool(set(user_groups) & set(bound_groups))


if __name__ == "__main__":
    unittest.main()
