"""The standing token revoker's policy, identity, volume, Deployment and install order.

These tests read the role as text and render its templates with Ansible's templating, so run
them with the Python of Ansible. Behaviour of the script itself is in test_openbao_revoker.py.
"""
import json
import re
import unittest
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar, trust_as_template

ROOT = Path(__file__).resolve().parent.parent
ROLE = ROOT / "roles/stack/operator/openbao"
NETPOL = ROOT / "roles/base/network-policies"
PROMETHEUS = ROOT / "roles/base/prometheus"
PLAYBOOK = ROOT / "playbooks/vertical-security/100-openbao.yml"
MAX_LOGIN_TTL_SECONDS = 5 * 60
UNITS = {"s": 1, "m": 60, "h": 3600}

init_plugin_loader()
LOADER = DataLoader()
DEFAULTS = LOADER.load_from_file(str(ROLE / "defaults/main.yml"))
CATALOGUE = yaml.safe_load((ROLE / "files/catalogue.yml").read_text())
RECORD_NODE = "node-a"


def templar(**extra):
    """A templar whose variables are the role defaults with every `{{ }}` reference resolved."""
    variables = {**DEFAULTS, "role_path": str(ROLE), "dmf_storage_class": "local-path",
                 "openbao_record_node": RECORD_NODE, **extra}
    for _ in range(4):
        step = Templar(loader=LOADER, variables=variables)
        variables = {
            key: step.template(trust_as_template(value)) if isinstance(value, str) and "{{" in value else value
            for key, value in variables.items()
        }
    return Templar(loader=LOADER, variables=variables)


def render_template(name, **extra):
    text = (ROLE / "templates" / name).read_text()
    out = templar(**extra).template(trust_as_template(text))
    return out if isinstance(out, dict) else yaml.safe_load(out)


def expression(text, **variables):
    return templar(**variables).template(trust_as_template(text))


def tasks(name):
    return yaml.safe_load((ROLE / "tasks" / name).read_text())


def seconds(duration):
    return int(duration[:-1]) * UNITS[duration[-1]]


def policy_grants(text):
    """The path blocks of an HCL policy as {path: sorted capabilities}."""
    found = {}
    for path, body in re.findall(r'path "([^"]+)"\s*\{([^}]*)\}', text):
        found[path] = sorted(re.findall(r'"(\w+)"', re.search(r"capabilities\s*=\s*\[([^\]]*)\]", body).group(1)))
    return found


class Policy(unittest.TestCase):
    def test_the_policy_grants_exactly_the_three_accessor_paths(self):
        # Catches a policy that grants more (a wildcard, revoke-self, a token create), omits the
        # sudo that the listing of accessors needs, or names another path.
        text = (ROLE / "files/policies/token-revoker.hcl").read_text()
        self.assertEqual(
            policy_grants(text),
            {
                "auth/token/accessors": ["list", "sudo"],
                "auth/token/lookup-accessor": ["update"],
                "auth/token/revoke-accessor": ["update"],
            },
        )
        self.assertEqual(len(re.findall(r'(?m)^path "', text)), 3)

    def test_the_policy_name_is_the_one_the_fake_checks(self):
        # Catches a rename of the policy that the fake and the role no longer agree on.
        self.assertEqual(DEFAULTS["openbao_revoker_policy_name"], "token-revoker")

    def test_the_main_play_writes_the_policy_before_the_role_that_names_it(self):
        main = (ROLE / "tasks/main.yml").read_text()
        write = main.index("- name: Write the token-revoker policy")
        self.assertLess(write, main.index("include_tasks: setup-revoker.yml"))
        self.assertIn("files/policies/token-revoker.hcl", main)


class Role(unittest.TestCase):
    def role(self):
        return {key: expression(value) for key, value in DEFAULTS["openbao_revoker_k8s_auth_role"].items()}

    def test_the_role_maps_the_serviceaccount_to_the_revoker_policy_only(self):
        role = self.role()
        self.assertEqual(role["policy"], "token-revoker")
        self.assertEqual(set(role) & {"policies", "token_policies"}, set())
        self.assertEqual(role["service_account"], "token-revoker")
        self.assertEqual(role["namespace"], "openbao")
        self.assertEqual(role["audience"], "openbao")

    def test_the_role_has_the_keys_of_the_executor_role(self):
        # Catches a role mapping that the shared writer (kubernetes-auth-role.yml) cannot read.
        self.assertEqual(set(DEFAULTS["openbao_revoker_k8s_auth_role"]), set(DEFAULTS["openbao_executor_k8s_auth_role"]))

    def test_the_login_token_is_short_and_bounded(self):
        role = self.role()
        self.assertLessEqual(seconds(role["token_ttl"]), MAX_LOGIN_TTL_SECONDS)
        self.assertLessEqual(seconds(role["token_max_ttl"]), MAX_LOGIN_TTL_SECONDS)
        self.assertLessEqual(seconds(role["token_ttl"]), seconds(role["token_max_ttl"]))

    def test_both_install_paths_write_the_role(self):
        # The standing list feeds the installed-environment path, setup-revoker.yml the fresh install.
        self.assertIn("{{ openbao_revoker_k8s_auth_role }}", DEFAULTS["openbao_standing_k8s_auth_roles"])
        setup = (ROLE / "tasks/setup-revoker.yml").read_text()
        self.assertIn('openbao_k8s_auth_role: "{{ openbao_revoker_k8s_auth_role }}"', setup)
        self.assertIn("kubernetes-auth-role.yml", setup)
        self.assertNotIn("bao write", setup)

    def test_the_setup_creates_an_identity_a_volume_a_configmap_and_a_stopped_deployment(self):
        kinds = [t["kubernetes.core.k8s"]["definition"] for t in tasks("setup-revoker.yml") if "kubernetes.core.k8s" in t]
        self.assertEqual(kinds[0]["kind"], "ServiceAccount")
        rendered = [d for d in kinds[1:]]
        self.assertEqual(len(rendered), 3)


class Render(unittest.TestCase):
    def deployment(self, **extra):
        return render_template("revoker-deployment.yml.j2", **extra)

    def pod(self, **extra):
        return self.deployment(**extra)["spec"]["template"]["spec"]

    def test_the_record_volume_is_mounted_read_only_twice(self):
        # Catches a claim mounted read-write: the volume level and the mount level both say
        # read-only, so neither a bad mount nor a bad volume spec lets the revoker write.
        pod = self.pod()
        volume = next(v for v in pod["volumes"] if v["name"] == "record")
        self.assertEqual(volume["persistentVolumeClaim"], {"claimName": "approval-executor-record", "readOnly": True})
        mount = next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "record")
        self.assertTrue(mount["readOnly"])
        self.assertEqual(mount["mountPath"], "/record")
        writable = [m["name"] for m in pod["containers"][0]["volumeMounts"] if not m.get("readOnly")]
        self.assertEqual(writable, [])

    def test_the_pod_lands_on_the_node_that_holds_the_record(self):
        # Catches a Deployment with no node constraint: on a local-path volume the pod could not
        # start on another node, and on a replicated volume it could be refused the attach. The
        # executor's pod takes the same value (openbao_record_node) in the workload slice.
        self.assertEqual(self.pod()["nodeSelector"], {"kubernetes.io/hostname": RECORD_NODE})
        self.assertEqual(self.pod(openbao_record_node="node-b")["nodeSelector"], {"kubernetes.io/hostname": "node-b"})

    def test_the_default_node_is_the_first_control_host(self):
        node = expression(DEFAULTS["openbao_record_node"], groups={"k3s_control": ["host-1", "host-2"]})
        self.assertEqual(node, "host-1")

    def test_an_empty_node_is_refused_by_the_setup(self):
        first = tasks("setup-revoker.yml")[0]
        self.assertIn("openbao_record_node | length > 0", first["ansible.builtin.assert"]["that"])

    def test_the_claim_is_one_readwriteonce_volume_on_the_site_storage_class(self):
        claim = render_template("record-pvc.yml.j2")
        self.assertEqual(claim["kind"], "PersistentVolumeClaim")
        self.assertEqual(claim["metadata"]["name"], "approval-executor-record")
        self.assertEqual(claim["metadata"]["namespace"], "openbao")
        self.assertEqual(claim["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(claim["spec"]["storageClassName"], "local-path")
        self.assertEqual(render_template("record-pvc.yml.j2", dmf_storage_class="longhorn")["spec"]["storageClassName"], "longhorn")

    def test_the_deployment_is_one_stopped_replica_with_recreate(self):
        # The play starts it only after the install (revoker-resume.yml).
        spec = self.deployment()["spec"]
        self.assertEqual(spec["replicas"], 0)
        self.assertEqual(spec["strategy"], {"type": "Recreate"})

    def test_the_container_is_locked_down(self):
        pod = self.pod()
        container = pod["containers"][0]
        self.assertEqual(container["securityContext"]["allowPrivilegeEscalation"], False)
        self.assertEqual(container["securityContext"]["readOnlyRootFilesystem"], True)
        self.assertEqual(container["securityContext"]["capabilities"], {"drop": ["ALL"]})
        self.assertEqual(pod["securityContext"]["runAsNonRoot"], True)
        self.assertEqual(pod["securityContext"]["runAsUser"], 65534)
        self.assertEqual(pod["automountServiceAccountToken"], False)
        self.assertEqual(container["image"], "python:3.13-alpine@sha256:9ef1a599f863231c3d91e941389bfdc4a2d6070557243d54df1a4e57015b960e")
        self.assertEqual(set(container["resources"]), {"requests", "limits"})
        self.assertIn("memory", container["resources"]["limits"])
        self.assertIn("cpu", container["resources"]["limits"])

    def test_the_login_token_is_projected_for_the_openbao_audience_and_stored_nowhere(self):
        pod = self.pod()
        projected = next(v for v in pod["volumes"] if v["name"] == "openbao-token")["projected"]["sources"]
        self.assertEqual(projected[0]["serviceAccountToken"]["audience"], "openbao")
        self.assertEqual(pod["serviceAccountName"], "token-revoker")
        # Only the CA certificate of the OpenBao Secret is mounted, never the key.
        secret = next(v for v in pod["volumes"] if v["name"] == "openbao-ca")["secret"]
        self.assertEqual(secret["items"], [{"key": "ca.crt", "path": "ca.crt"}])
        writable = [v for v in pod["volumes"] if "emptyDir" in v or "hostPath" in v]
        self.assertEqual(writable, [])

    def test_the_script_and_the_decided_values_reach_the_pod(self):
        env = {e["name"]: e["value"] for e in self.pod()["containers"][0]["env"]}
        self.assertEqual(env["REVOKER_GRACE_SECONDS"], "300")
        self.assertEqual(env["REVOKER_SCAN_INTERVAL_SECONDS"], "60")
        self.assertEqual(env["REVOKER_RECORD_DIR"], "/record")
        address = urlsplit(env["REVOKER_BAO_ADDR"])
        self.assertEqual(
            (address.scheme, address.hostname, address.port),
            ("https", "openbao.openbao.svc.cluster.local", 8200),
        )
        self.assertEqual(self.pod()["containers"][0]["command"], ["python", "/config/revoker.py"])

    def test_the_pod_is_scraped_on_its_metrics_port(self):
        annotations = self.deployment()["spec"]["template"]["metadata"]["annotations"]
        self.assertEqual(annotations["prometheus.io/scrape"], "true")
        self.assertEqual(annotations["prometheus.io/port"], "9102")
        self.assertEqual(annotations["prometheus.io/path"], "/metrics")

    def test_the_configmap_carries_the_script_and_the_catalogue_policy_names(self):
        # Catches a ConfigMap that alters the script's bytes, and a name list that drifts from the
        # catalogue file: the revoker's pod cannot mount the catalogue's own ConfigMap.
        data = render_template("revoker-configmap.yml.j2")["data"]
        self.assertEqual((ROLE / "files/revoker/revoker.py").read_bytes(), data["revoker.py"].encode())
        names = json.loads(data["catalogue-policies.json"])
        self.assertEqual(names, [e["policy_name"] for e in CATALOGUE["entries"]])
        self.assertTrue(names)
        self.assertTrue(all(n.startswith("catalogue-") for n in names))

    def test_the_script_uses_only_the_standard_library(self):
        source = (ROLE / "files/revoker/revoker.py").read_text()
        imports = set(re.findall(r"(?m)^(?:import|from) (\w+)", source))
        import sys
        self.assertTrue(imports <= set(sys.stdlib_module_names), imports - set(sys.stdlib_module_names))


class NetworkPolicy(unittest.TestCase):
    def test_the_revoker_reaches_the_api_port_only(self):
        found = [t["kubernetes.core.k8s"]["definition"] for t in yaml.safe_load((NETPOL / "tasks/main.yml").read_text())
                 if t["kubernetes.core.k8s"]["definition"]["metadata"]["name"] == "openbao-allow-from-token-revoker"]
        self.assertEqual(len(found), 1)
        spec = found[0]["spec"]
        self.assertEqual(spec["policyTypes"], ["Ingress"])
        rule = spec["ingress"][0]
        self.assertEqual(len(spec["ingress"]), 1)
        self.assertEqual(rule["from"], [{"podSelector": {"matchLabels": {
            "app.kubernetes.io/name": "{{ network_policies_openbao_allow_revoker_pod_label }}"}}}])
        self.assertEqual(rule["ports"], [{"protocol": "TCP", "port": 8200}])
        self.assertNotIn("egress", spec)

    def test_the_label_matches_the_name_of_the_deployment(self):
        defaults = yaml.safe_load((NETPOL / "defaults/main.yml").read_text())
        self.assertEqual(defaults["network_policies_openbao_allow_revoker_pod_label"], DEFAULTS["openbao_revoker_name"])
        pod = render_template("revoker-deployment.yml.j2")["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(pod["app.kubernetes.io/name"], defaults["network_policies_openbao_allow_revoker_pod_label"])


class Alert(unittest.TestCase):
    def test_the_rules_use_the_metrics_that_the_script_serves(self):
        # Catches a rename on one side only.
        rules = (PROMETHEUS / "templates/rules/alerts.yaml.j2").read_text()
        script = (ROLE / "files/revoker/revoker.py").read_text()
        for metric in ("dmf_openbao_revoker_record_readable", "dmf_openbao_revoker_last_scan_timestamp_seconds",
                       "dmf_openbao_revoker_failures"):
            self.assertIn(metric, rules)
            self.assertIn(metric, script)
        for alert in ("OpenBaoRevokerRecordUnreadable", "OpenBaoRevokerNotScanning", "OpenBaoRevokerScanFailing"):
            self.assertIn(f"alert: {alert}", rules)


class Order(unittest.TestCase):
    """The revoker never scans while a root token of the install is live."""

    main = tasks("main.yml")
    names = [t["name"] for t in main]

    def index(self, name):
        return self.names.index(name)

    def first_using_a_root_token(self):
        for number, task in enumerate(self.main):
            text = yaml.safe_dump(task)
            if any(marker in text for marker in ("operator init", "token create", "root_token")):
                return number
        self.fail("no task makes or uses a root token")

    def test_the_revoker_stops_only_on_a_fresh_init_and_before_the_init_makes_a_token(self):
        # Catches a pause that comes after the init, which would leave a window in which the
        # revoker scans while the init token is live, and a pause on every run, which would
        # drop the revoker for a rerun that makes no root token.
        pause = self.index("Stop the revoker before the init makes a root token")
        self.assertLess(pause, self.first_using_a_root_token())
        self.assertLess(pause, self.index("Run Shamir init"))
        self.assertEqual(self.main[pause]["ansible.builtin.include_tasks"], "revoker-pause.yml")
        self.assertEqual(self.main[pause]["when"], "not openbao_is_initialized | bool")
        flagged = tasks("revoker-pause.yml")[0]
        self.assertEqual(flagged["ansible.builtin.set_fact"], {"_openbao_revoker_paused": True})

    def test_the_pause_scales_to_zero_and_then_waits_for_the_pods_to_end(self):
        pause = tasks("revoker-pause.yml")
        order = [t["name"] for t in pause]
        scale = next(t for t in pause if "kubernetes.core.k8s_scale" in t)
        self.assertEqual(scale["kubernetes.core.k8s_scale"]["replicas"], 0)
        wait = next(t for t in pause if t["name"] == "Wait until no revoker pod is left")
        self.assertLess(order.index(scale["name"]), order.index(wait["name"]))
        self.assertEqual(wait["kubernetes.core.k8s_info"]["kind"], "Pod")
        self.assertIn("length == 0", wait["until"])
        self.assertIn("app.kubernetes.io/name={{ openbao_revoker_name }}", wait["kubernetes.core.k8s_info"]["label_selectors"])

    def test_the_revoker_starts_only_after_the_installer_token_is_shown_revoked(self):
        resume = self.index("Start the revoker once the installer's tokens are revoked")
        self.assertGreater(resume, self.index("Require the short-lived write token to be revoked"))
        self.assertGreater(resume, self.index("Revoke the short-lived write token"))
        for later in self.main[resume + 1:]:
            text = yaml.safe_dump(later)
            self.assertNotIn("_openbao_write_token", text)
            self.assertNotIn("_openbao_short_token", text)
            self.assertNotIn("root_token", text)

    def test_only_the_resume_file_scales_the_revoker_up(self):
        # Catches a second place that starts the revoker, or a Deployment template that renders
        # one replica: a rerun would then leave it scanning while the installer's token is live.
        for path in list(ROLE.rglob("*.yml")) + list(ROLE.rglob("*.j2")):
            text = path.read_text()
            if path.name == "revoker-resume.yml":
                self.assertIn("replicas: 1", text)
            elif "revoker" in text:
                self.assertNotRegex(text, r"(?m)^\s*replicas:\s*[1-9]", str(path))

    def test_the_resume_runs_only_after_a_pause_and_only_when_both_tokens_are_proved_revoked(self):
        # The resume runs only when the run stopped the revoker, and starts it only when this
        # expression is true: the init token and the short token are both proved revoked.
        resume = next(t for t in self.main if t["name"] == "Start the revoker once the installer's tokens are revoked")
        self.assertEqual(resume["when"], "_openbao_revoker_paused | default(false) | bool")
        text = resume["vars"]["openbao_revoker_installer_token_revoked"]
        for task in tasks("revoker-resume.yml"):
            if "kubernetes.core.k8s_scale" in task:
                self.assertIn("openbao_revoker_installer_token_revoked | bool", task["when"])
        for init_gone in (True, False):
            for short_gone in (True, False):
                with self.subTest(init_gone=init_gone, short_gone=short_gone):
                    allowed = str(expression(text, _openbao_init_gone=init_gone, _openbao_short_gone=short_gone)).strip()
                    self.assertEqual(allowed == "True", init_gone and short_gone)

    def test_the_resume_waits_for_a_ready_replica_and_fails_without_one(self):
        # Catches a resume that scales up and goes on: a revoker that cannot log in would pass.
        resume = tasks("revoker-resume.yml")
        names = [t["name"] for t in resume]
        wait = resume[names.index("Wait until the revoker has finished a scan")]
        stop = resume[names.index("Stop when the revoker has not finished a scan")]
        self.assertGreater(names.index(wait["name"]), names.index("Scale the revoker to one replica"))
        self.assertIn("readyReplicas", wait["until"])
        self.assertIn("ansible.builtin.fail", stop)
        self.assertIn("readyReplicas", " ".join(stop["when"]))

    def test_the_resume_fails_when_the_deployment_is_missing_unless_the_rescue_allows_it(self):
        # Catches a resume that skips the scale and the wait for a Deployment that is gone and
        # returns success: the install would then finish without a running revoker. The rescue
        # of a failed run can run before the setup created the Deployment, and fails anyway.
        resume = {t["name"]: t for t in tasks("revoker-resume.yml")}
        missing = resume["Stop when the revoker Deployment is missing"]
        order = list(resume)
        self.assertLess(order.index(missing["name"]), order.index("Scale the revoker to one replica"))
        msg = missing["ansible.builtin.fail"]["msg"]
        self.assertIn("is missing", msg)
        self.assertIn("setup-revoker.yml", msg)
        cases = (
            # revoked, Deployments found, rescue allows a missing one, the task fails
            (True, [], False, True),
            (True, [{}], False, False),
            (True, [], True, False),
            (False, [], False, False),
        )
        for revoked, found, may_be_missing, fails in cases:
            with self.subTest(revoked=revoked, found=len(found), may_be_missing=may_be_missing):
                variables = {
                    "openbao_revoker_installer_token_revoked": revoked,
                    "openbao_revoker_may_be_missing": may_be_missing,
                    "_openbao_revoker_to_start": {"resources": found},
                }
                fires = all(expression("{{ " + c + " }}", **variables) for c in missing["when"])
                self.assertEqual(fires, fails)
        playbook = PLAYBOOK.read_text()
        resume_call = playbook.index("tasks_from: revoker-resume.yml")
        self.assertIn("openbao_revoker_may_be_missing: true", playbook[resume_call:resume_call + 600])
        main_call = next(t for t in self.main if "revoker-resume.yml" in yaml.safe_dump(t))
        self.assertNotIn("openbao_revoker_may_be_missing", yaml.safe_dump(main_call))

    def test_an_apply_of_an_existing_deployment_keeps_the_replica_count(self):
        # Catches a setup that resets the revoker to zero on every run.
        new = render_template("revoker-deployment.yml.j2")["spec"]
        kept = render_template("revoker-deployment.yml.j2", openbao_revoker_deployment_exists=True)["spec"]
        self.assertEqual(new["replicas"], 0)
        self.assertNotIn("replicas", kept)
        setup = tasks("setup-revoker.yml")
        apply = next(t for t in setup if t["name"] == "Create or update the revoker Deployment")
        self.assertIn("_openbao_revoker_deployment.resources", apply["vars"]["openbao_revoker_deployment_exists"])

    def test_the_pod_is_ready_only_after_a_complete_scan(self):
        probe = render_template("revoker-deployment.yml.j2")["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
        self.assertEqual(probe["httpGet"]["path"], "/ready")

    def test_a_failed_run_leaves_the_revoker_stopped_and_only_a_finished_run_starts_it(self):
        # In the main play the resume task sits outside every block that a failure could run
        # through. In the rescue of the playbook it comes only after the assert that proves
        # the install's tokens revoked, so a failed proof ends the rescue with the revoker
        # still stopped, and a mint whose answer was lost keeps it stopped too.
        playbook = PLAYBOOK.read_text()
        self.assertEqual(playbook.count("tasks_from: revoker-resume.yml"), 1)
        resume = playbook.index("tasks_from: revoker-resume.yml")
        self.assertLess(playbook.index("- name: Assert failed-bootstrap root disposal succeeded"), resume)
        self.assertLess(resume, playbook.index("- name: Re-fail after fresh-root cleanup"))
        self.assertIn("_openbao_failed_mint_unread", playbook[resume:resume + 400])
        self.assertEqual(sum("revoker-resume.yml" in yaml.safe_dump(t) for t in self.main), 1)
        resume_task = next(t for t in self.main if "revoker-resume.yml" in yaml.safe_dump(t))
        self.assertNotIn("block", resume_task)


if __name__ == "__main__":
    unittest.main()
