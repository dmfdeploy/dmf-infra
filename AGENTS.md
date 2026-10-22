# AGENTS.md — dmf-infra

This repo holds the generic Ansible playbooks and roles that provision and
operate a k3s cluster for the dmfdeploy platform. Environment values are
operator-local and reach it through `dmf-env` (ADR-0035). Umbrella: https://github.com/dmfdeploy/dmfdeploy

## Boot steps

1. In the umbrella repo, run `git pull` and `bin/generate-status.sh`, then read
   `STATUS.digest.local.md`. Post shared notes to the pinned issue
   https://github.com/dmfdeploy/dmfdeploy/issues/618
2. Apply any fitting ADR from the umbrella `docs/decisions/INDEX.md`. For cluster
   ops or secrets, read section 0 of the matching umbrella `.agents/skills/` skill.

## Build and test

From `k3s-lab-bootstrap/`, run the CI checks: `yamllint .`, `ansible-lint playbooks/ -p`,
`ansible-playbook playbooks/<name>.yml --syntax-check`, `helm lint charts/<chart>/`.
Run playbooks from `dmf-env`:
`bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/playbooks/<name>.yml`

## Rules that are easy to miss

- Never commit secrets, real IPs, hostnames or absolute paths. Use `vault_*` and
  inventory variables; derive URLs from `external_base_url`. Store persistent
  tokens in OpenBao, never in ansible-vault files.
- Never run a playbook with `ansible-playbook` (`--syntax-check` is fine); use the
  `dmf-env` wrapper, and never change the wrapper without explicit guidance.
- Never rely on a local `kubectl`. Use playbook tasks, or `sudo k3s kubectl`
  on the control node. Never run `lnms` as root (`su` to the `librenms` user).
- Keep roles idempotent. Name tasks imperatively; use `snake_case` variables
  prefixed with the role name. Match existing playbook numbering and role layout.
- Do not use `--tags` or `--skip-tags` as a workaround; fix the sequencing. Put
  work too complex for Ansible in a role or a one-shot playbook, not a shell
  script. Check dependencies before you add a utility: the repo runs anywhere.
- Include `roles/base/cluster-ready` before app roles. For NetBox v4 tokens and
  landing-page rollouts, see `k3s-lab-bootstrap/docs/`.

<!-- WORKING-MODEL-BLOCK-START — generated from umbrella docs/templates/working-model-block.md; do not edit copies, edit the template and run bin/check-working-model-sync.sh -->
## Working model (mandatory)

Canonical: [docs/WORKING-MODEL.md](https://github.com/dmfdeploy/dmfdeploy/blob/main/docs/WORKING-MODEL.md)
in the umbrella repo. The three rules that matter mid-task:

1. **Work starts at an issue** in the canonical backlog
   ([dmfdeploy/dmfdeploy issues](https://github.com/dmfdeploy/dmfdeploy/issues)):
   `component:*`/`workstream:*` labels **always**, plus a milestone **only if
   the work is scheduled** — unscheduled work gets `platform-debt` and no
   milestone (§2). Non-trivial work gets a plan doc in umbrella `docs/plans/`
   with `tracking_issue` frontmatter.
2. **The completing PR auto-closes its issue; you still flip the plan
   frontmatter by hand in that PR.** Reference umbrella issues **fully
   qualified** — `Closes dmfdeploy/dmfdeploy#N` (bare `#N` targets the wrong
   repo); the daily issue-close reconciler honors that ref, cross-repo
   included. Manual close is a fallback.
3. **Never invent a local backlog** (TODO files, ad-hoc trackers). Issues =
   liveness; plan frontmatter = design state; ADRs = decisions (RFC in
   Discussions first); STATUS.md = pointer file (operator notes live in pinned issue dmfdeploy/dmfdeploy#618);
   STATUS.local.md = live repo snapshot.
<!-- WORKING-MODEL-BLOCK-END -->
