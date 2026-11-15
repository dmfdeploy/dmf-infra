"""Token availability for the AWX integration reads of in-cluster OpenBao.

The NetBox and Forgejo runtime reads use the operator login token. This test loads
tasks/main.yml, renders the `when:` conditions of the tasks involved with Jinja2, the
engine that Ansible uses, and walks every combination of these inputs:

- the break-glass file is set or empty,
- the NetBox, Forgejo, catalog and purge tokens are supplied or missing.

For each combination it checks four properties:

1. A read runs exactly when a token it fetches is missing. The catalog and purge
   tokens come from the NetBox read, and only when the break-glass file is set.
2. When the play reaches a read, the break-glass file, the pod lookup and the operator
   login all ran first, and the read sends the operator token on stdin.
3. When no token is missing, no read runs and no set_fact task overwrites a supplied
   token.
4. Without the break-glass file, a missing token stops the play at the assertion before
   any read.

The `bool` filter is Ansible's own.
"""
import itertools
import pathlib
import unittest

import jinja2
import yaml
from ansible.plugins.filter.core import FilterModule

TASKS_FILE = pathlib.Path(__file__).resolve().parent.parent / "tasks/main.yml"
TASKS = yaml.safe_load(TASKS_FILE.read_text(encoding="utf-8"))

PERSIST = "Set AWX secret persistence mode"
BREAKGLASS_LOAD = "Load OpenBao break-glass JSON for AWX integration"
LOGIN_ASSERT = "Assert the operator login is available for AWX source-token reads"
OPERATOR_CREDENTIALS = "Extract OpenBao operator credentials for AWX persistence"
POD_FACT = "Set OpenBao pod fact for AWX integration"
OPERATOR_LOGIN = "Log into OpenBao with operator userpass for AWX persistence"
NETBOX_READ = "Read NetBox runtime token from in-cluster OpenBao"
FORGEJO_READ = "Read Forgejo runtime token from in-cluster OpenBao"
NETBOX_SET = "Set NetBox awx inventory token from in-cluster OpenBao"
FORGEJO_SET = "Set Forgejo token from in-cluster OpenBao"

OPERATOR_TOKEN = "operator-token-value"
# A token from any other variable would render as one of these.
OTHER_TOKENS = {"_awx_openbao_client_token": "approle-token-value"}

ENV = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
ENV.filters["bool"] = FilterModule().filters()["bool"]


def task(name):
    found = [t for t in TASKS if t.get("name") == name]
    if len(found) != 1:
        raise AssertionError(f"expected one task named {name!r}, found {len(found)}")
    return found[0]


def module_args(t):
    for key, value in t.items():
        if key.startswith("ansible.builtin."):
            return value
    raise AssertionError(f"task {t.get('name')!r} has no builtin module")


def holds(expressions, context):
    """Evaluate a `when:` or `that:` value as Ansible does: every item must hold."""
    if expressions is None:
        return True
    if not isinstance(expressions, list):
        expressions = [expressions]
    for expression in expressions:
        if isinstance(expression, bool):
            result = expression
        else:
            result = ENV.compile_expression(str(expression).strip())(**context)
        if not result:
            return False
    return True


def run_play(file, netbox, forgejo, catalog, purge):
    """Return what runs for one combination of inputs, in play order."""
    context = {
        "awx_integration_openbao_breakglass_file": file,
        "awx_integration_netbox_token": netbox,
        "awx_integration_forgejo_token": forgejo,
        "awx_integration_netbox_catalog_token": catalog,
        "awx_integration_netbox_purge_token": purge,
    }
    persist_template = module_args(task(PERSIST))["awx_integration_persist_to_openbao"]
    context["awx_integration_persist_to_openbao"] = ENV.filters["bool"](
        ENV.from_string(persist_template).render(**context)
    )
    ran = {}
    for name in (BREAKGLASS_LOAD, OPERATOR_CREDENTIALS, POD_FACT):
        ran[name] = holds(task(name).get("when"), context)
    login_assert = task(LOGIN_ASSERT)
    ran[LOGIN_ASSERT] = holds(login_assert.get("when"), context)
    ran["assert_fails"] = ran[LOGIN_ASSERT] and not holds(module_args(login_assert)["that"], context)
    ran[OPERATOR_LOGIN] = holds(task(OPERATOR_LOGIN).get("when"), context)
    after_login = dict(context)
    if ran[OPERATOR_LOGIN]:
        after_login["_awx_openbao_operator_client_token"] = OPERATOR_TOKEN
    for name in (NETBOX_READ, FORGEJO_READ):
        ran[name] = not ran["assert_fails"] and holds(task(name).get("when"), after_login)
        stdin = module_args(task(name))["stdin"]
        values = dict(after_login, **OTHER_TOKENS)
        try:
            ran[name + " stdin"] = ENV.from_string(stdin).render(**values)
        except jinja2.UndefinedError:
            ran[name + " stdin"] = None
    # The set_fact tasks run only when the read registered a result.
    with_result = dict(after_login)
    with_result["_awx_openbao_netbox_raw"] = {"stdout": "{}"}
    with_result["_awx_openbao_forgejo_raw"] = {"stdout": "{}"}
    ran[NETBOX_SET] = ran[NETBOX_READ] and holds(task(NETBOX_SET).get("when"), with_result)
    ran[FORGEJO_SET] = ran[FORGEJO_READ] and holds(task(FORGEJO_SET).get("when"), with_result)
    return ran


class SourceTokenFlowTest(unittest.TestCase):
    def combinations(self):
        for file, netbox, forgejo, catalog, purge in itertools.product(
            ("", "/keys/breakglass.json"), ("", "nb"), ("", "fg"), ("", "cat"), ("", "purge")
        ):
            label = (
                f"file={'set' if file else 'empty'} netbox={netbox or '-'} forgejo={forgejo or '-'} "
                f"catalog={catalog or '-'} purge={purge or '-'}"
            )
            yield label, (file, netbox, forgejo, catalog, purge), run_play(file, netbox, forgejo, catalog, purge)

    def test_a_read_runs_exactly_when_a_token_it_fetches_is_missing(self):
        for label, (file, netbox, forgejo, catalog, purge), ran in self.combinations():
            if ran["assert_fails"]:
                continue
            with self.subTest(label):
                netbox_needed = not netbox or (bool(file) and (not catalog or not purge))
                self.assertEqual(ran[NETBOX_READ], netbox_needed)
                self.assertEqual(ran[FORGEJO_READ], not forgejo)

    def test_a_read_that_runs_has_the_operator_login_before_it(self):
        order = [t.get("name") for t in TASKS]
        for name in (NETBOX_READ, FORGEJO_READ):
            self.assertLess(order.index(OPERATOR_LOGIN), order.index(name))
            self.assertLess(order.index(POD_FACT), order.index(name))
            self.assertLess(order.index(BREAKGLASS_LOAD), order.index(OPERATOR_LOGIN))
        for label, _, ran in self.combinations():
            for name in (NETBOX_READ, FORGEJO_READ):
                if not ran[name]:
                    continue
                with self.subTest(label, read=name):
                    for before in (BREAKGLASS_LOAD, OPERATOR_CREDENTIALS, POD_FACT, OPERATOR_LOGIN):
                        self.assertTrue(ran[before], f"{before} did not run before {name}")

    def test_a_read_that_runs_sends_the_operator_token_on_stdin(self):
        for label, _, ran in self.combinations():
            for name in (NETBOX_READ, FORGEJO_READ):
                if not ran[name]:
                    continue
                with self.subTest(label, read=name):
                    self.assertEqual(ran[name + " stdin"], OPERATOR_TOKEN + "\n")

    def test_supplied_tokens_skip_the_reads_and_are_kept(self):
        for label, (file, netbox, forgejo, catalog, purge), ran in self.combinations():
            with self.subTest(label):
                if netbox:
                    self.assertFalse(ran[NETBOX_SET])
                if forgejo:
                    self.assertFalse(ran[FORGEJO_SET])
                if netbox and forgejo and catalog and purge:
                    self.assertFalse(ran[NETBOX_READ])
                    self.assertFalse(ran[FORGEJO_READ])
                    self.assertFalse(ran["assert_fails"])

    def test_a_missing_token_without_the_break_glass_file_stops_before_any_read(self):
        for label, (file, netbox, forgejo, catalog, purge), ran in self.combinations():
            if file or (netbox and forgejo):
                continue
            with self.subTest(label):
                self.assertTrue(ran["assert_fails"])
                self.assertFalse(ran[NETBOX_READ])
                self.assertFalse(ran[FORGEJO_READ])

    def test_with_every_token_supplied_no_read_runs_and_the_login_still_persists(self):
        ran = run_play("/keys/breakglass.json", "nb", "fg", "cat", "purge")
        self.assertFalse(ran[NETBOX_READ])
        self.assertFalse(ran[FORGEJO_READ])
        # The login still runs: the role persists its own tokens with it.
        self.assertTrue(ran[OPERATOR_LOGIN])


if __name__ == "__main__":
    unittest.main()
