# OpenBao Policy Reconciliation Agent Prompt

Use this prompt for a freshly cleared agent taking over the OpenBao policy
reconciliation and bootstrap-hardening work in this repository.

```text
You are working in <repos>/dmf-infra.

Goal:
Fix the OpenBao bootstrap and policy reconciliation path so fresh bootstrap and
normal reruns both work after the security hardening changes. Do not tear down
or reprovision the lab cluster unless explicitly asked. Implement repo-side
fixes first, then validate.

Critical context:
- This repo is generic/environment-agnostic. Do not commit real IPs or secrets.
- Environment-specific configuration lives in <repos>/dmf-env.
- The DMF model is:
  - dmf-env holds inventory, Resource Profile intent, and secret references.
  - Runtime cluster/app secrets live in each cluster's embedded OpenBao.
  - External Secrets Operator materializes OpenBao values into Kubernetes
    Secrets for workloads.
  - The OpenBao root token is an init-only bootstrap credential, not a durable
    env value.
  - Break-glass unseal/share material is offline escrow, not normal runtime KV.
- Do not add an initial/root token to env values as the normal fix. If an
  existing initialized cluster has no policy-reconciliation identity and no
  valid admin identity, require an explicit one-time recovery token from the
  operator via no_log/stdin only, or document that a clean rebuild is required.
- Check git status before editing. Do not revert unrelated user changes.

Read these first:
- docs/openbao-policy-reconciliation-agent-prompt.md (this file)
- docs/SECURITY-REMEDIATION-GUIDE.md, especially N-1 audit-log leakage notes
- k3s-lab-bootstrap/roles/stack/operator/openbao/defaults/main.yml
- k3s-lab-bootstrap/roles/stack/operator/openbao/tasks/main.yml
- k3s-lab-bootstrap/roles/common/app-admin-facts/tasks/main.yml
- k3s-lab-bootstrap/playbooks/vertical-security/100-openbao.yml
- k3s-lab-bootstrap/playbooks/vertical-security/191-zot-oidc.yml
- k3s-lab-bootstrap/playbooks/vertical-orchestration/100-eso.yml
- k3s-lab-bootstrap/playbooks/runbooks/eso-openbao-health-check.yml
- <repos>/dmf-env/inventories/hetzner-arm/group_vars/all/openbao.yml
- <repos>/dmf-env/inventories/hetzner-arm/group_vars/all/eso.yml
- <repos>/dmf-env/bin/export-openbao-vars.sh
- <repos>/dmfdeploy/docs/architecture/DMF Platform Plan.md
- <repos>/dmfdeploy/docs/plans/DMF Secret Ownership and OpenBao Migration Plan.md
- <repos>/dmfdeploy/docs/plans/DMF Deployment Workflow and Manifest Plan.md

Current diagnosis from review:
The earlier assumption that OpenBao 2.5.2 has a root-token permission bug is
probably wrong. The more likely regression is from the N-1 security hardening
commit that moved secrets out of inline kubectl exec shell commands and into
Ansible task-level environment variables.

Why that broke bootstrap:
- In Ansible, task `environment:` applies to the local process that launches
  `kubectl`.
- It does not become the environment of the process created inside the pod by
  `kubectl exec`.
- Therefore tasks like:
    kubectl exec openbao-0 -- bao policy write ...
  with:
    environment:
      BAO_TOKEN: "{{ openbao_root_token }}"
  run the in-pod `bao` command without BAO_TOKEN.
- That explains 403 "permission denied" from policy writes and auth/engine
  configuration, even though the root token itself is expected to be powerful.
- Some auth/engine tasks use failed_when: false, so failures can be masked until
  a later policy write or downstream app secret write fails.

Audit/logging constraint:
- Do not simply revert to inline:
    sh -c "BAO_TOKEN='...' bao ..."
  because that can put credentials in Kubernetes audit logs or process argv.
- Also do not use:
    kubectl exec -- env BAO_TOKEN=... bao ...
  as the final fix. The secret is still in the exec command vector/request URI.
- Preferred pattern: pass credentials over stdin into a single in-pod shell,
  read them inside the pod, export BAO_TOKEN only in memory, run bao, and exit.
- There is an existing working pattern to copy from:
  k3s-lab-bootstrap/playbooks/696-cms-authentik-api.yml, task
  "Persist API token to OpenBao".
- Keep no_log: true on any task that handles tokens, passwords, secret IDs, or
  payloads.

OpenBao policy mechanics to keep in mind:
- Policies grant capabilities on paths.
- KV v2 uses `secret/data/...` for secret value read/write and
  `secret/metadata/...` for list/metadata operations.
- `create` and `update` are distinct KV v2 write capabilities; use both where
  a task may seed or modify an existing document.
- ESO uses AppRole and should receive the narrow `eso-reader` policy.
- ops-admin is the human/operator bootstrap identity for app admin/runtime
  secret reads/writes, not a sys/policies administrator.
- policy-reconciler is the machine/operator reconciliation identity for ACL
  policy writes on reruns.

Known current code state to verify before editing:
- defaults/main.yml already contains separate policy names in current HEAD:
  - openbao_operator_policy_name: app-admin-writer
  - openbao_operator_reader_policy_name: app-admin-reader
  - openbao_operator_runtime_policy_name: app-runtime-writer
  - openbao_eso_policy_name: eso-reader
  - openbao_policy_writer_policy_name: policy-writer
  - openbao_policy_reconciler_username: policy-reconciler
- The policy clobber bug was partly fixed:
  - app-admin-writer and app-admin-reader now have separate temp files/names.
  - eso-reader body exists again.
- The reconciliation objective was not actually completed:
  - policy HCL rendering is still gated on openbao_init_raw.changed in several
    places.
  - policy writes are still gated on openbao_init_raw.changed.
  - policy-reconciler creation is still gated on openbao_init_raw.changed.
  - ops-admin capability checks are still gated on openbao_init_raw.changed.
  - Reruns against an already initialized OpenBao therefore cannot repair live
    policy drift.
- Many authenticated OpenBao calls still rely on Ansible `environment:` for
  BAO_TOKEN or passwords while executing `bao` inside the pod. Those are broken
  unless the command itself moves the value into the pod process.

Environment repo findings:
- <repos>/dmf-env/inventories/hetzner-arm/group_vars/all/openbao.yml
  sets the OpenBao break-glass automation JSON path.
- <repos>/dmf-env/inventories/hetzner-arm/group_vars/all/eso.yml
  may still document/use a legacy HTTP URL, but
  vertical-orchestration/100-eso.yml normalizes legacy http://openbao... to the
  TLS service URL.
- <repos>/dmf-env/inventories/hetzner-arm/group_vars/all/openbao_secrets.yml
  contains historical operator-side OpenBao/AppRole metadata. The current
  export-openbao-vars.sh deliberately does not read OpenBao pre-bootstrap; it
  reads local bootstrap shims and generates ephemeral bootstrap values.
- Do not treat openbao_secrets.yml as the solution for OpenBao policy
  reconciliation.

Implementation objectives:
1. Restore correct credential delivery into the in-pod bao process without
   reintroducing audit-log/argv leaks.
2. Keep root token use init-only.
3. Make policy-as-code rerun-reconcilable through a narrow policy-reconciler
   identity.
4. Keep policy names distinct and non-clobbering.
5. Make failure modes explicit and actionable.
6. Preserve the DMF secret ownership model: runtime secrets in OpenBao, env repo
   contains references/metadata only, break-glass material remains offline.

Recommended implementation sequence:

1. Add a safe in-pod OpenBao exec pattern.
   - Prefer a small local pattern/helper within the role rather than ad hoc
     one-off command strings everywhere.
   - The pattern should:
     - use kubectl exec -i into the OpenBao pod;
     - pass token/password/payload through stdin;
     - inside the pod, read the credential from stdin;
     - export BAO_ADDR=https://127.0.0.1:8200;
     - export BAO_CACERT=/openbao/config/tls/tls.crt if needed;
     - export BAO_TOKEN only in the in-pod shell;
     - run the intended bao command;
     - exit without writing durable token files unless intentionally using a
       short-lived temporary file that is cleaned up in the same shell.
   - Do not use `kubectl exec -- env BAO_TOKEN=...`.
   - Do not place root tokens, client tokens, passwords, secret IDs, or KV
     payload values in argv.

2. Fix fresh-init authenticated OpenBao calls.
   - Audit every task in openbao/tasks/main.yml that calls `bao` after init.
   - For root-token tasks, pass openbao_root_token via stdin into the pod.
   - Keep Shamir init, custody writes, unseal, and root-token revocation on
     init-only gates.
   - For auth method and secrets engine enablement, stop masking errors with
     broad failed_when: false.
   - Accept only the known "already enabled/path already in use" cases as
     idempotent. Fail on 403, missing token, TLS errors, or malformed commands.

3. Separate policy rendering from policy application.
   - Render/write policy HCL files into the OpenBao pod whenever the pod is
     available and unsealed, not only on fresh init.
   - Keep temp filenames clear:
     - /tmp/app-admin-writer-policy.hcl
     - /tmp/app-admin-reader-policy.hcl
     - /tmp/app-runtime-writer-policy.hcl
     - /tmp/eso-reader-policy.hcl
     - /tmp/born-inventory-runtime-reader-policy.hcl
     - /tmp/policy-writer-policy.hcl
   - Ensure the HCL bodies are not logged if they include sensitive path
     structure beyond generic policy, but no_log is mandatory for tasks carrying
     credentials.

4. Implement the policy token selection flow.
   - Define a fact like `_openbao_policy_apply_token`.
   - Fresh init:
     - Use openbao_root_token before revocation.
     - Create/repair policy-writer policy.
     - Create/repair policy-reconciler userpass identity.
   - Rerun:
     - Load policy_reconciler_username/password from break-glass JSON.
     - Log in as policy-reconciler using stdin password passing.
     - Use returned client token via stdin pattern for policy writes.
   - If rerun lacks policy-reconciler credentials:
     - fail clearly before app rollouts:
       "OpenBao is already initialized but policy-reconciler credentials are not
       present in break-glass JSON. Provide a one-time privileged recovery token
       via an explicit no_log/stdin recovery variable or rebuild OpenBao from a
       clean bootstrap."
     - Do not silently generate a new password on rerun unless the role can
       also update the live OpenBao user using an already authorized token.

5. Apply policy-as-code on every rerun.
   - Use `_openbao_policy_apply_token` to write:
     - openbao_operator_policy_name -> app-admin-writer HCL
     - openbao_operator_reader_policy_name -> app-admin-reader HCL, if retained
     - openbao_operator_runtime_policy_name -> app-runtime-writer HCL
     - openbao_eso_policy_name -> eso-reader HCL
     - openbao_born_inventory_policy_name -> born-inventory reader HCL
     - openbao_policy_writer_policy_name -> policy-writer HCL
   - Keep policy-writer minimal:
     - sys/policies/acl/*
     - add sys/policies/acl read/list only if OpenBao requires that exact path
       for list/read behavior.
     - Do not grant broad sys/*.
   - Do not grant ops-admin sys/policies/acl/*.

6. Ensure identities are correct.
   - ops-admin should receive:
     - app-admin-writer
     - app-runtime-writer
   - It should not be downgraded by app-admin-reader.
   - app-admin-reader should only exist if still needed for a separate read-only
     human or automation identity.
   - ESO AppRole should use token_policies={{ openbao_eso_policy_name }} rather
     than a hardcoded eso-reader string where practical.
   - born-inventory AppRole remains narrow to NetBox runtime read.
   - policy-reconciler should receive only policy-writer.

7. Fix downstream OpenBao command patterns.
   - common/app-admin-facts currently logs in and performs KV reads/writes using
     environment values that do not reach the pod. Convert it to the stdin
     single-exec pattern.
   - Then inspect and prioritize other roles/playbooks that run `kubectl exec`
     into the OpenBao pod with task-level BAO_TOKEN/OPENBAO_PASSWORD:
     - authentik
     - awx-integration
     - cms
     - forgejo-bootstrap
     - netbox
     - netbox-sot
     - dmf-born-inventory
     - 696/697 playbooks
     - ESO rotation runbook
   - Do not broaden scope too much in the first patch if that risks a large,
     hard-to-review change. At minimum, fix the OpenBao role and
     common/app-admin-facts because they block the policy pipeline and Zot admin
     secret persistence.

8. Add precise post-checks.
   - On both fresh init and rerun after policy application:
     - log in as ops-admin using stdin password passing;
     - check capabilities for:
       - secret/data/apps/zot/admin
       - secret/metadata/apps/zot/admin
     - assert data path includes read plus create/update as appropriate;
     - assert metadata path includes read/list as appropriate.
   - Fail with an explicit policy reconciliation message, not a generic command
     failure.
   - If possible, also check a generic app admin wildcard path and a runtime path
     used by Authentik/AWX/NetBox.

9. Improve app-admin-facts failure reporting.
   - Register KV read/write results with failed_when: false only where followed
     immediately by explicit interpretation.
   - Detect 403/permission denied/deny and emit:
     "OpenBao denied write to <path>. This usually means stale or unreconciled
     app-admin-writer policy on ops-admin. Rerun OpenBao policy reconciliation
     or perform the documented one-time recovery."
   - Preserve no_log for secrets while still providing enough non-secret context
     to identify the failed path.

10. Keep Zot OIDC validation before rollout.
   - vertical-security/191-zot-oidc.yml should validate that
     secret/apps/zot/admin was persisted/readable before restarting Zot.
   - Its validation must authenticate correctly and should not rely on a missing
     local environment variable inside kubectl exec.

11. Update runbooks/docs as needed.
   - Update eso-openbao-health-check.yml to use the corrected login/token
     pattern.
   - Document manual verification:
     - log in as ops-admin;
     - bao token capabilities secret/data/apps/zot/admin;
     - bao token capabilities secret/metadata/apps/zot/admin;
     - bao kv get secret/apps/zot/admin.
   - Document that stale policy is repaired by policy-reconciler, not by storing
     a root token in env values.

Validation plan:
1. Before running anything:
   - git status --short
   - git diff --check
   - inspect diff for accidental secrets/IPs.
2. Static checks:
   - ansible-playbook syntax check via the env wrapper when possible:
     cd <repos>/dmf-env
     bin/run-playbook.sh hetzner-arm ../dmf-infra/k3s-lab-bootstrap/playbooks/vertical-security/100-openbao.yml --syntax-check
     bin/run-playbook.sh hetzner-arm ../dmf-infra/k3s-lab-bootstrap/playbooks/vertical-security/191-zot-oidc.yml --syntax-check
     bin/run-playbook.sh hetzner-arm ../dmf-infra/k3s-lab-bootstrap/playbooks/vertical-orchestration/100-eso.yml --syntax-check
3. Non-destructive live validation, only if asked/approved:
   - run 100-openbao.yml against the existing cluster;
   - confirm policies apply on rerun without root;
   - confirm ops-admin capabilities on Zot admin path;
   - run app-admin-facts path through 191-zot-oidc.yml;
   - confirm ESO ClusterSecretStore remains Ready.
4. Do not run destructive commands:
   - no PVC deletion;
   - no OpenBao re-init;
   - no cluster teardown;
   - no root-token persistence into env repo.

Expected final state:
- Fresh bootstrap initializes, unseals, enables engines/auth methods, writes
  policies, creates AppRoles/users, and revokes root token successfully.
- Reruns can re-render and reapply policy-as-code through policy-reconciler.
- eso-reader, app-admin-writer, app-admin-reader if retained,
  app-runtime-writer, born-inventory reader, and policy-writer remain distinct.
- ops-admin can read/create/update app admin secrets including
  secret/apps/zot/admin.
- ESO reads app/admin/bootstrap paths with the eso-reader AppRole.
- common/app-admin-facts reports policy drift clearly.
- No root token or durable initial token is added to dmf-env values.
- Credential-bearing kubectl exec calls no longer depend on Ansible
  task-level environment variables reaching the pod.
- Credential-bearing values do not appear in kubectl exec argv/request URIs.
```
