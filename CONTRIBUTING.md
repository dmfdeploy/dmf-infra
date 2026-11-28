# Contributing to dmf-infra

Generic Ansible playbooks and roles for the dmfdeploy platform.

This repo is part of the **dmfdeploy platform**. GitHub is the canonical home and the
single source of truth: all changes land via **Pull Request** against `main`.

## Quick start

1. Read the platform overview in the [dmfdeploy/dmfdeploy](https://github.com/dmfdeploy/dmfdeploy) umbrella repo
   (`docs/architecture/dmfdeploy-platform-plan.md`) and apply any relevant ADRs from
   `docs/decisions/INDEX.md`.
2. Set up your machine and run the checks (see [Develop and test](#develop-and-test)).
3. Fork or branch, make your change on a topic branch, open a PR against `main`.
4. Ensure CI is green, your commits are **signed off** (see DCO below), and the
   PR body links its issue (see [What a pull request needs](#what-a-pull-request-needs)).

## Develop and test

This section is the one place for development and test commands. The README and
`AGENTS.md` point here. The commands match what CI runs in
`.github/workflows/ci.yml`. Where CI doesn't run a command, the text says so.

### Prerequisites

- **Python 3.14.** CI runs Python 3.14 (the `python-version` setting in
  `ci.yml`). Use 3.14 to match it.
- **Network access to Ansible Galaxy**, to install the collections.
- **[Helm](https://helm.sh/).** The chart lint runs it. CI installs it with
  `azure/setup-helm`.
- **[skopeo](https://github.com/containers/skopeo).** One fixture playbook, the
  Zot index mirror test, runs it. CI installs it with `apt-get`. Without skopeo
  that playbook fails, and it doesn't skip. Install it with your package
  manager, for example `brew install skopeo` or `sudo apt-get install skopeo`.
- **[promtool](https://prometheus.io/docs/prometheus/latest/command-line/promtool/).**
  The alert rule tests run it. CI downloads a pinned release and verifies its
  checksum.
- **Optional tools.** [gitleaks](https://github.com/gitleaks/gitleaks) powers
  the commit hook.

You don't need a cluster, a cloud account or secrets for any check in this
section. The fixture playbooks run against a fake `kubectl` and fake collections
under `k3s-lab-bootstrap/tests/fixtures/`. Running a playbook against real
nodes needs more. See [Checks that need more than a checkout](#checks-that-need-more-than-a-checkout).

### Set up

Run these commands from the repository root. The block stops at the first
failing command and returns a failure status.

```bash
(
  set -euo pipefail
  python3 --version
  python3 -m venv .venv
  cd k3s-lab-bootstrap
  ../.venv/bin/python -m pip install -r requirements-ansible-core.txt \
    -r requirements-ci.txt -r requirements.txt
  ../.venv/bin/ansible-galaxy collection install -r ee/requirements.yml
)
```

When the block succeeds, activate the virtual environment. Do this again in each
new terminal:

```bash
. .venv/bin/activate
```

`requirements-ansible-core.txt` and `requirements-ci.txt` pin `ansible-core`,
`ansible-lint` and `yamllint`, the same files that CI installs.
`requirements.txt` declares the Python packages that the
playbooks and the unit tests import. `jmespath` is one: the `json_query` filter
in `community.general` needs it, and tests that use the filter fail without it.
`ansible-galaxy` installs the collections into `~/.ansible/collections`, outside
the virtual environment, where other projects can see them. The second list in
the `ansible-galaxy` command holds the collections that the AWX execution
environment bakes in. CI installs both lists.

### Run the checks

Run each block in this section from the repository root, with the virtual
environment active. Each block changes to `k3s-lab-bootstrap/` itself, stops at
the first failing command and returns a failure status. The commands cover
a subset of the CI checks. See [What CI runs](#what-ci-runs).

Lint, and run the unit tests:

```bash
(
  set -euo pipefail
  cd k3s-lab-bootstrap
  yamllint .
  ansible-lint playbooks/ -p
  python3 -m unittest discover -s roles/stack/operator/awx-integration/tests -v
  python3 tests/test-forgejo-contributor-policy.py
  python3 -m unittest discover -s tests -p 'test_audit_forgejo_identities.py' -v
  python3 -m unittest discover -s tests -p 'test_openbao_*.py' -v
  python3 -m unittest discover -s tests -p 'test_coredns_custom_zone.py' -v
  python3 -m unittest discover -s tests -p 'test_local_ca_node_address.py' -v
  python3 -m unittest discover -s tests -p 'test_no_log_secrets.py' -v
  python3 -m unittest discover -s tests -p 'test_secret_jobs_no_trace_mode.py' -v
  python3 -m unittest discover -s tests -p 'test_secrets_not_in_argv_render.py' -v
  python3 -m unittest discover -s tests -p 'test_cms_values_trust_info.py' -v
)
```

Check the syntax of every playbook:

```bash
(
  set -euo pipefail
  cd k3s-lab-bootstrap
  for pb in playbooks/*.yml; do
    echo "=== syntax-check: $pb ==="
    ansible-playbook "$pb" --syntax-check
  done
)
```

Run the OpenBao fixture playbooks. This block matches the CI step named
"OpenBao Raft render, guard, unseal and migration tests" in
`.github/workflows/ci.yml`. The first two playbooks run no cluster commands, so
CI runs them without the fakes. The
`export` lines then select the fake `kubectl` and the fake `kubernetes.core`
collection for every playbook after them, and the subshell keeps them out of
your shell:

```bash
(
  set -euo pipefail
  cd k3s-lab-bootstrap
  ansible-playbook tests/test-openbao-ha-render.yml
  ansible-playbook tests/test-openbao-ha-guard.yml
  export PATH="$PWD/tests/fixtures/fake-kubectl:$PATH"
  export ANSIBLE_COLLECTIONS_PATH="$PWD/tests/fixtures/fake-collections:$HOME/.ansible/collections"
  ansible-playbook tests/test-openbao-ha-guard-collect.yml
  ansible-playbook tests/test-openbao-guard-poll.yml
  ansible-playbook tests/test-openbao-unseal.yml
  ansible-playbook tests/test-openbao-data-read.yml
  ansible-playbook tests/test-openbao-capabilities.yml
  ansible-playbook tests/test-openbao-restart-guard.yml
  ansible-playbook tests/test-openbao-grow-ha.yml
  ansible-playbook tests/test-openbao-k8s-auth.yml
  ansible-playbook tests/test-openbao-raft-snapshot.yml
  ansible-playbook tests/test-openbao-cert-issuance.yml
  ansible-playbook tests/test-openbao-ca-bundle.yml
  ansible-playbook tests/test-openbao-init-status.yml
  ansible-playbook tests/test-openbao-tls-secret-wait.yml
)
```

The fixture playbooks stay off real systems only when you run them with this
invocation. Without the `export` lines, a fixture playbook calls the real
`kubectl` and the real collection, and the roles it includes read
`/etc/rancher/k3s/k3s.yaml`. Never run one of these playbooks on a machine that
has a cluster's kubeconfig, such as `/etc/rancher/k3s/k3s.yaml`, unless you
select the fakes the same way.

Run the other fixture playbooks that CI runs. None of them calls a cluster
command. The `test-zot-seed-index-mirror.yml` playbook needs skopeo. The last
three use only local facts and assertions, so they need no fakes:

```bash
(
  set -euo pipefail
  cd k3s-lab-bootstrap
  ansible-playbook tests/test-harden-fail2ban-ignoreip.yml
  ansible-playbook tests/test-zot-seed-index-mirror.yml
  ansible-playbook tests/test-app-admin-facts-live-read.yml
  ansible-playbook tests/test-authentik-oauth-credentials-parse.yml
  ansible-playbook tests/test-k3s-nat-bind-ip.yml
)
```

Run the AWX autoscale helper tests. They need `pytest`, which
`k3s-lab-bootstrap/requirements.txt` declares, so the earlier install step
covers it:

```bash
(
  set -euo pipefail
  cd k3s-lab-bootstrap/roles/stack/operator/awx-autoscale
  python -m pytest tests -q
)
```

Run the Prometheus alert rule tests. They need
[promtool](https://prometheus.io/docs/prometheus/latest/command-line/promtool/).
The alert template calls the filters of the AWX identity role, so the block
points `ANSIBLE_FILTER_PLUGINS` at them. It renders the template twice, once for
the default AWX identity and once for a second identity that the tower rule
tests use. The files go to a place that Git ignores. The block then checks both
rule files and runs each test file:

```bash
(
  set -euo pipefail
  cd k3s-lab-bootstrap/roles/base/prometheus
  export ANSIBLE_FILTER_PLUGINS="$PWD/../../common/awx-identity/filter_plugins"
  ansible localhost -m ansible.builtin.template \
    -a "src=templates/rules/alerts.yaml.j2 dest=tests/rendered_alerts.yml" \
    -e @defaults/main.yml -e awx_namespace=awx -e awx_instance_name=awx
  ansible localhost -m ansible.builtin.template \
    -a "src=templates/rules/alerts.yaml.j2 dest=tests/rendered_alerts_tower.yml" \
    -e @defaults/main.yml -e awx_namespace=lab -e awx_instance_name=tower
  promtool check rules tests/rendered_alerts.yml tests/rendered_alerts_tower.yml
  for f in tests/test_*.promtool.yml; do
    promtool test rules "$f"
  done
)
```

Lint the Helm charts:

```bash
(
  set -euo pipefail
  cd k3s-lab-bootstrap
  for chart in charts/*/; do
    echo "=== helm-lint: $chart ==="
    helm lint "$chart"
  done
)
```

Outside the fixture tests, run `ansible-playbook` directly only for
`--syntax-check`. Run each fixture playbook only with the invocation shown
earlier, and every other playbook through the `dmf-env` wrapper, which exports
the secrets from OpenBao first.

These fixture instructions are for people and for CI. An AI coding agent
follows `AGENTS.md`, which allows `ansible-playbook` only with
`--syntax-check`. An agent leaves the fixture playbooks to CI or to a
maintainer, and says so in the pull request.

The times below are ranges from a laptop. The whole run takes 30 to 40 minutes.
CI runs related checks in parallel jobs: `ansible-checks`, which allows 15
minutes; `ansible-unit`, which splits OpenBao unit tests into 3 shards; and
`ansible-openbao`, which splits fixture playbooks into 5 shards. CI also runs
checks not listed here.

| Check | Time |
| --- | --- |
| `yamllint` | A few seconds |
| `ansible-lint` | About 25 seconds |
| Syntax check of every playbook | About 20 seconds |
| The `awx-integration`, Forgejo, CoreDNS, `no_log` and trace mode tests | A few seconds each |
| The OpenBao unit tests, `test_openbao_*.py` | 15 to 25 minutes |
| `test_local_ca_node_address.py` | About 20 seconds |
| `test-openbao-k8s-auth.yml` | About 4.5 minutes |
| `test-openbao-grow-ha.yml` | About 3.5 minutes |
| `test-openbao-cert-issuance.yml` | About 2 minutes |
| Every other fixture playbook | Under 40 seconds each |
| `helm lint` | A few seconds |

Before you push, run the lint, the syntax check, and the unit tests. Then run the
fixture playbook for the role you changed. If you didn't change OpenBao
`k8s-auth`, HA growth or certificate issuance, you can leave those slow
playbooks to CI.

### What CI runs

The `ci` workflow has these jobs:

- **`ansible-checks`** installs the pinned `ansible-core`, `ansible-lint`,
  `yamllint` and `requirements.txt` on Python 3.14, then runs lint, syntax,
  chart and unit checks; the previous section lists a local subset. It also
  installs Helm and skopeo.
- **`review-comments`** runs `bin/check-review-comments.sh` and its
  `--self-test`. The check rejects a code comment that records review history.
  The self-test takes about 20 seconds.
- **`working-model`** confirms that the working-model block in `AGENTS.md`,
  `CLAUDE.md` and `QWEN.md` matches the umbrella template. It needs a checkout
  of the umbrella repo, so it runs in CI only. Edit the block only through the
  template in the umbrella repo.

To run the review-comment check, run this block from the repository root:

```bash
(
  set -euo pipefail
  bin/check-review-comments.sh
  bin/check-review-comments.sh --self-test
)
```

The `guard` workflow scans for secrets and vulnerabilities, lints the workflow
files, and checks commit subjects and the issue link. Its gitleaks job scans the
checked-out files with the `.gitleaks.toml` of the base branch, and it skips the
commit history. The `DCO` workflow checks sign-offs. Both run in CI only.

### Checks that need more than a checkout

CI doesn't run these. Run them when your change touches what they cover.

| Check | What it covers | Needs |
| --- | --- | --- |
| `ee/scripts/build.sh` | The AWX execution environment image | Docker and `ansible-builder`. See [ee/README.md](k3s-lab-bootstrap/ee/README.md). |
| Any playbook against real nodes | The playbooks and roles on a cluster | Nodes you can reach over SSH, OpenBao access and an environment from [dmf-env](https://github.com/dmfdeploy/dmf-env). Run the playbook through the `dmf-env` wrapper. |

A green run of the preceding checks doesn't prove a change works on a cluster. If
your change needs a live check, say so in the pull request and ask a maintainer
to run it.

### Install the hooks

Run this once in each clone:

```bash
bin/install-hooks.sh
```

The script sets `core.hooksPath` to `.githooks/`. The `pre-commit` hook scans
your staged changes with gitleaks. The `pre-push` hook scans the commits you
push, and it also reads a `.gitleaks.local.toml` file when you have one. Both
hooks skip the scan with a warning when gitleaks isn't installed, so install
gitleaks to turn the scans on. The CI scan is different. It reads the
checked-out files with the base branch's configuration, and it doesn't read your
commit history.

### Evidence in proportion to the change

Match the proof to the change.

- **A documentation change** needs no test run. The PR gates check the rest.
  Install gitleaks and the hooks so that your own machine scans the change for
  secrets before CI does.
- **A change to a role, playbook or script** needs a test that fails without the
  change and passes with it. Add a fixture playbook or a unit test, and paste
  the runner's own summary line, such as the closing `OK` of a `unittest` run,
  in the pull request.
- **A fix for a defect that other code could share** needs a search for the
  same pattern. Search the repository, then say which matches you checked and
  which you changed.
- **A change that only a cluster can prove** needs a note in the pull request:
  what you ran, where and what you saw. If you couldn't run it, say that.

### Representative tasks

| Task | Where | Offline or live |
| --- | --- | --- |
| Fix a defect in the OpenBao rotation logic and add a unit test | `k3s-lab-bootstrap/lib/`, `k3s-lab-bootstrap/tests/test_openbao_*.py` | Offline |
| Change a role task and cover it with a fixture playbook | `k3s-lab-bootstrap/roles/`, `k3s-lab-bootstrap/tests/test-*.yml` | Offline |
| Edit a Helm chart or a role template | `k3s-lab-bootstrap/charts/`, `k3s-lab-bootstrap/roles/` | Offline for lint and render. Live to confirm on a cluster. |
| Change a Prometheus alert rule | `k3s-lab-bootstrap/roles/base/prometheus/` | Offline with `promtool`. CI runs the rule tests. |
| Add or change a playbook that configures nodes | `k3s-lab-bootstrap/playbooks/` | Offline for lint and syntax. Live to run it. |
| Rebuild the AWX execution environment image | `k3s-lab-bootstrap/ee/` | Live. It needs Docker and, to publish, a registry token. |

## Branch & PR model

- **GitHub Pull Requests only.** Direct push to `main` is blocked; force-push is
  banned; linear history is required.
- Topic branches: **`<handle>/<short-slug>`** (e.g. `jdoe/fix-probe-path`). One
  logical change per branch; rebase onto `main` rather than long-lived branches.
- A reviewer (per `CODEOWNERS`) and green required checks are needed to merge.

## What a pull request needs

CI checks each of these.

- **An issue link in the PR body.** Write a fully qualified reference to an issue
  in the [dmfdeploy/dmfdeploy](https://github.com/dmfdeploy/dmfdeploy/issues)
  backlog, such as `Refs dmfdeploy/dmfdeploy#912`. Use `Closes dmfdeploy/dmfdeploy#N`
  if the pull request finishes the issue. A bare `#N` doesn't count, because it
  points at this repo. If your change has no issue, such as a typo fix, a
  maintainer can add the `no-issue` label. Only maintainers can add labels, so
  if you open the pull request from a fork, ask in the PR. The check runs when
  you open the pull request, reopen it, push a commit, edit the PR body, or add
  or remove a label (the `on:` trigger list in `issue-link.yml`). After a
  maintainer adds `no-issue`, nothing more is needed.
- **A DCO sign-off on every commit.** Use `git commit -s`. See the next section.
- **Conventional commit subjects.** Use `type(scope): summary`, with one
  optional scope. The scope may use only `a-z`, `0-9`, `.`, `_` and `-`. The
  types are `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `build`, `ci`,
  `perf`, `revert` and `style`. An `!` before the colon marks a breaking change.
  Other types, such as `wip`, are rejected, and so is a scope list such as
  `(api,ui)`.
- **Passing CI.** The `ci` workflow runs the checks in
  [Develop and test](#develop-and-test). The `guard` workflow adds secret and
  vulnerability scans.

Also leave agent session links out of commit messages and the PR body. The
`guard` workflow rejects one known session-link format. It can't recognize
every tool's links, so reviewers look for them too.

## Developer Certificate of Origin (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/),
not a CLA. **Every commit must be signed off:**

```bash
git commit -s -m "fix: correct the probe path"
```

This appends a `Signed-off-by: Your Name <you@example.com>` trailer certifying you
have the right to submit the work under the project license. A **DCO check** runs
on every PR and fails if any commit is missing the trailer. Amend with
`git commit --amend -s` or rebase with `git rebase --signoff main` to fix. PRs are
**rebase-merged** by default so your signed-off commits land on `main` unchanged.

## Versioning & releases

This repo carries a `VERSION` file (single semver line). Per **ADR-0005**,
`VERSION` is the single source of truth — any release-tagged change must update it
in the same commit. **No VERSION bump → no release.** Release tags are `v<VERSION>`,
created by release automation, never by hand.

## Secrets & public-safety posture

**Secrets stay in OpenBao.** Never commit, track, or reference credentials, tokens,
keys, kubeconfigs, or Terraform state — not even with a "remove later" TODO. Use
**placeholder syntax** for any IPs, DNS names, or operator identity in code, docs,
PR descriptions, or issues (`<control-node-public-ip>`, `dmf.example.com`,
`<handle>`). A local pre-commit gitleaks hook runs on commit, and CI runs
secret-scanning + scrub gates on every PR — but redaction is your responsibility
first. If you need a secret, ask a maintainer — do not improvise a transport.

## Must / Must not

### MUST
- Open changes as **GitHub PRs against `main`** with **signed-off** commits.
- Use Conventional Commit messages and `<handle>/<short-slug>` topic branches.
- Update `VERSION` in the same commit as any release-tagged change.
- Use **placeholder syntax** for all IPs / DNS / operator identity in every artifact.
- Keep roles generic — no env-specific values; placeholder syntax for all cluster
  identifiers.
- Keep roles idempotent, follow the numbering of the existing playbooks, and
  document a complex deployment, as `k3s-lab-bootstrap/docs/netbox-deployment-notes.md`
  does.

### MUST NOT
- Commit secrets, tokens, keys, kubeconfigs, or Terraform state.
- Push directly to `main`, force-push, or use `--no-verify` / `--no-gpg-sign`.
- Paste secrets, real IPs/DNS, or operator identity into issues, PRs, or CI logs.

## AI agent contract

Much of this platform is built by AI agents. Agents contribute the same way:
**PRs against `main`, signed off, CI green.** Additionally, agents must run cluster
mutation only via `bin/run-playbook.sh` (ADR-0010), must not use
`--no-verify`/`--force`/`--no-gpg-sign`, and must stop and ask before modifying a
sub-repo with uncommitted state.

## Reporting security issues

See [`SECURITY.md`](SECURITY.md). **Do not** open a public issue for a vulnerability.

## License & spec

Contributions are licensed under [Apache 2.0](LICENSE). The canonical governance
model is **ADR-0041 — DMF Release and Contribution Model** in the
[dmfdeploy/dmfdeploy](https://github.com/dmfdeploy/dmfdeploy)
umbrella repo (`docs/decisions/`).
