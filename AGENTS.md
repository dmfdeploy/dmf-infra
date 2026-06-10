# dmf-infra — AI Agent Rules

## DMF Platform context — read first

This repo is a component of the **DMF Platform**, an umbrella workspace at
`$DMFDEPLOY_UMBRELLA/`. Cross-cutting state (status, decisions, plans,
skills) lives there, not here.

Before any non-trivial change in this repo:

```bash
cd "$DMFDEPLOY_UMBRELLA"
git fetch && git pull
bin/generate-status.sh --no-fetch    # refreshes STATUS.md
```

Then read in order:
1. `dmfdeploy/STATUS.md` — what's happening across all repos right now
2. `dmfdeploy/CLAUDE.md` — full boot ritual + workspace map
3. `dmfdeploy/docs/decisions/INDEX.md` — ADRs applicable to your task
4. The most recent file under `dmfdeploy/docs/handoffs/`

For cluster ops, secrets, or dmf-cms releases, also read §0 Secrets Discipline
of the relevant skill in `dmfdeploy/.claude/skills/`.

If you change cross-repo state, update the `<!-- HUMAN-START -->` section of
`dmfdeploy/STATUS.md` before ending the session.

---

## Agent Guidance for Infrastructure Code

### Playbook Structure

**Playbook organization** follows the EBU DMF Reference Architecture lifecycle stages:

```
k3s-lab-bootstrap/
├── playbooks/
│   ├── 1xx/         # Layer 1: Bare metal baseline (200–219)
│   ├── 2xx/         # Layer 2: OS hardening (210–219)
│   ├── 3xx/         # Layer 3: Kubernetes + networking (300–339)
│   ├── 4xx/         # Layer 4: Storage & scheduling (330+)
│   ├── 5xx/         # Layer 5: Platform services (600+)
│   ├── 6xx/         # Layer 6: Applications (650+)
│   ├── vertical-security/     # Cross-cutting: security & secrets (100–191)
│   ├── vertical-monitoring/   # Cross-cutting: observability (100–130)
│   ├── vertical-orchestration/ # Cross-cutting: automation (100–120)
│   └── lifecycle-*            # Lifecycle orchestrators (provision, operate, finalise)
```

**See:** `dmfdeploy/docs/architecture/DMF EBU Mapping (2026-04-25).md` for the full layer/vertical/lifecycle reference.

### Ansible Conventions

**Idempotency:**
- All roles and playbooks must be safe to re-run multiple times without changing state.
- Use `changed_when: false` for read-only tasks (facts gathering, validation).
- Use `when:` guards to skip tasks if the desired state already exists.

**Task naming:**
- Task names are imperative and describe the desired outcome, not the action.
- Example: ✓ "Create nginx namespace" (not ✗ "Create the namespace")

**Role structure:**
```
roles/my-role/
├── defaults/main.yml       # Default variables (lowest precedence)
├── vars/main.yml           # Role-specific variables (higher precedence)
├── tasks/main.yml          # Main task sequence
├── handlers/main.yml       # Handlers for service restarts, reloads
├── templates/              # Jinja2 templates
├── files/                  # Static files
└── README.md               # Role documentation (optional but recommended)
```

**Variable naming:**
- Use `snake_case` for all variables.
- Prefix role variables with the role name: `nginx_worker_processes`, `postgresql_version`.
- Use `_temp` for intermediate variables that are not part of the interface.

### Paths and Imports

**All paths must use relative references or variables:**
- ✓ `../../../docs/architecture/...` (relative from playbook location)
- ✓ `{{ lookup('file', role_variable) }}` (variable-driven, set in inventory)
- ✗ `/Users/<operator>/...` (hardcoded absolute paths — forbidden)
- ✗ `~/repos/...` (hardcoded home paths — forbidden)

**Environment-specific config (IPs, URLs, secrets) lives in dmf-env inventory, not in playbooks.**

### Secrets & Credentials

**Never commit secrets to this repo.**
- Secrets are exported from OpenBao by `dmf-env/bin/run-playbook.sh` before Ansible starts.
- Reference secrets via `vault_*` variables in playbooks.
- Lock files, breakglass JSON, and API tokens are configurable via role variables and inventory.

**Example:**
```yaml
# ✓ Good: uses a variable with fallback
openbao_breakglass_file: "{{ eso_openbao_breakglass_file | default((openbao_key_path | default('')) ~ '.json', true) }}"

# ✗ Bad: hardcoded path
openbao_breakglass_file: <secure-store>/openbao-breakglass/<env>/openbao-keys-automation.json
```

### Kubernetes & Helm

**Never use local `kubectl` — playbooks run remotely via dmf-env wrapper:**
```bash
# ✓ Good: runs on the cluster via Ansible
bin/run-playbook.sh hetzner-arm ../dmf-infra/k3s-lab-bootstrap/playbooks/650-dmf-cms.yml

# ✗ Bad: assumes kubectl is wired up on the operator's Mac
kubectl get pods -A
```

**Helm & kubectl commands go inside playbook tasks with `kubernetes.core.*` modules or `ansible.builtin.command`.**

### Common Issues & Fixes

**Heredoc + shell module breaks with `cmd: >-`** (folded scalar):
- Folded scalars strip newlines, breaking heredocs.
- **Fix:** Use `cmd: |-` (literal block) or build the cmd as a variable first (see `698-cms-netbox-forgejo-tokens.yml`).

**`no_log: true` hides real errors:**
- Temporarily remove `no_log: true`, run the task, inspect the error, then restore it.

**NetBox v4 model surprises:**
- Don't include `is_staff`, `is_active`, or other Django User fields when creating users.
- NetBox v4 uses v2 tokens: the full token is `TOKEN_PREFIX + key + "." + secret`, and API calls use `Authorization: Bearer <full_token>`.

**Authentik runtime layout:**
- Use `ak shell -c '<python>'` for imperative access, but prefer declarative blueprints for config.
- Image runs Python at `python` (no venv path).

**Zot admin API:**
- `adminPolicy.users` only honors htpasswd accounts, not OIDC group membership.
- Workaround is pending (see `dmfdeploy/docs/handoffs/` for prior session notes).

### Testing & Validation

**Before submitting a PR:**
1. Syntax-check playbooks with `ansible-playbook --syntax-check`.
2. Run in `--check` mode if the repo has an environment wrapper.
3. Verify idempotency by running twice — no unexpected changes on the second run.
4. Review role and playbook comments for outdated references to secbrain (should reference `dmfdeploy/docs/` instead).

### When to Use Codex

Codex is particularly useful for:
- **Playbook review & optimization** — catch Ansible anti-patterns, idempotency issues, or unnecessary complexity.
- **Debugging failed tasks** — analyze error messages, suggest fixes.
- **Infrastructure diagnosis** — review logs, suggest remediation steps.
- **Bash command generation** — write shell commands for one-shot cluster diagnostics.
- **Documentation updates** — ensure playbook comments and README files stay current.

### What NOT to Do

- **Don't hardcode paths** — use relative paths or variables from inventory.
- **Don't invent new naming conventions** — match existing playbook numbering and role structure.
- **Don't add new utilities without checking dependencies** — dmf-infra is run-anywhere; minimize assumptions.
- **Don't use `--skip-tags` or `--tags` as a workaround** — fix the underlying sequencing instead.
- **Don't create imperative shell scripts as escapes** — if a task is too complex for Ansible, it belongs in a role or a well-documented one-shot playbook.
- **Don't touch the environment wrapper** (`dmf-env/bin/run-playbook.sh`) without explicit guidance — it's the only thing holding the multi-repo setup together.
