# QWEN.md — dmf-infra

## DMF Platform context — read first

This repo is a component of the **DMF Platform**, an umbrella workspace
checked out alongside this repo. Operators set `$DMFDEPLOY_UMBRELLA` to its
local path. Cross-cutting state (status, decisions, plans, skills) lives
there, not here.

Before any non-trivial change in this repo:

```bash
cd "$DMFDEPLOY_UMBRELLA"
git fetch && git pull
bin/generate-status.sh --no-fetch    # refreshes STATUS.md
```

Then read in order:
1. `dmfdeploy/STATUS.md` — what's happening across all 6 repos right now
2. `dmfdeploy/QWEN.md` — full boot ritual + skills index + Qwen-specific rules
3. `dmfdeploy/docs/decisions/INDEX.md` — ADRs applicable to your task
4. The most recent file under `dmfdeploy/docs/handoffs/`

For cluster ops, secrets, or dmf-cms releases, also read §0 Secrets Discipline
of the relevant skill in `dmfdeploy/.claude/skills/`. Qwen doesn't have
Claude's `/skill-name` invocation — read the SKILL.md as documentation
and apply its sections like instructions.

If you change cross-repo state, update the `<!-- HUMAN-START -->` section of
`dmfdeploy/STATUS.md` before ending the session.

---

## Repo-specific notes

This is the **public, generic** ansible repo (per ADR-0002). Strict rule:
**no real IPs, no passwords, no site-specific URLs** — anything sensitive
lives in `dmf-env` (the private inventory repo). Pre-commit / CI
checks for hardcoded values are still pending; the discipline is on you
for now.

Most operational work in this repo is changes to playbooks/roles. Run them
through `dmf-env/bin/run-playbook.sh` per ADR-0010. Never invoke
`ansible-playbook` directly.

For deeper guidance see `CLAUDE.md` in this repo (Claude Code's main
guidance file — comprehensive troubleshooting, common commands, layout).
The boot ritual ↑ supersedes anything in CLAUDE.md that conflicts.
