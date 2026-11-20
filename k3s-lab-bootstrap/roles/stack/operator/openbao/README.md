# openbao

Installs OpenBao on the cluster as a StatefulSet (one pod, or a Raft cluster),
initializes it, and writes the platform policies and identities. The defaults
with a comment on each variable are in `defaults/main.yml`. This page covers the
parts that need more than a comment: the image pin, the listener certificate
reload, root token generation, and the upgrade order.

## Image pin

`openbao_image` is a release tag pinned to the digest of the multi-arch index,
for example `docker.io/openbao/openbao:2.7.2@sha256:...`. The snapshot CronJob
(`roles/base/openbao-raft-snapshot`) runs the same reference through
`openbao_raft_snapshot_image`, so the cluster runs one release. Change both
together. To get the digest of a tag:

```bash
skopeo inspect --raw docker://docker.io/openbao/openbao:<tag> | shasum -a 256
```

The StatefulSet uses the `OnDelete` update strategy. A changed image restarts a
single pod by itself and restarts no pod of a Raft cluster. Restart a Raft
cluster one pod at a time with
`playbooks/vertical-security/103-openbao-restart-pod.yml`.

## Listener certificate reload

The listener sets `tls_auto_reload = true` (OpenBao 2.7 and later). The server
polls the certificate files, 30 seconds apart by default, and loads a renewed
certificate without a signal. `roles/base/cert-manager` still sends `SIGHUP` after
an issuance and checks that every pod serves the current certificate. Keep the
signal: pods before 2.7 ignore `tls_auto_reload` and need it, and it makes the
reload happen when the play runs instead of within the next polling interval.
Keep the check in any case, because it is the proof.

## Standing Kubernetes-auth identities

Four identities log in to OpenBao with a projected ServiceAccount token and hold
no stored credential. Each is a mapping in `openbao_standing_k8s_auth_roles`. A
fresh install writes the roles in `setup-issuer.yml`,
`setup-rotation-scheduler.yml`, `setup-executor.yml` and `setup-revoker.yml`. An installed
environment without them is rebuilt.

| Identity | ServiceAccount and role | Namespace | Policy | Login token |
|---|---|---|---|---|
| Setup issuer | `setup-issuer` | `openbao` | `setup-issuer` | 5 minutes, at most 10 |
| Rotation scheduler | `approle-rotation-scheduler` | `openbao` | `approle-rotation-scheduler` | 20 minutes, at most 30 |
| Approval executor | `approval-executor` | `openbao` | `generate-root` | 5 minutes, at most 10 |
| Token revoker | `token-revoker` | `openbao` | `token-revoker` | 2 minutes, at most 5 |

The approval executor has no workload until slice D builds its image. Until then
only its ServiceAccount, role and network rules exist. Its only policy is
`generate-root`. Two NetworkPolicies in `roles/base/network-policies` cover it:
one admits its pods, labelled `app.kubernetes.io/name: approval-executor`, to the
OpenBao API port. The other admits only pods inside the cluster to the executor.

## Token revoker

The token revoker is a standing Deployment in the `openbao` namespace. It ends the
tokens that the approval executor leaves behind, so that a failed or abandoned
change cannot leave a root-policy token valid.

### What it revokes

The script (`files/revoker/revoker.py`, standard library only) runs from a ConfigMap
on `python:3.13-alpine`, the image of the ntfy bridge in `roles/base/prometheus`.
Every 60 seconds it logs in with its projected ServiceAccount token, lists the
accessors of the live tokens, and looks each one up. It logs in again on every scan
and keeps no token between scans, so it stores no token on disk.

An *executor token* is a token whose policies include `root` or any policy named in
`files/catalogue.yml` (all start with `catalogue-`). The revoker leaves every
other token alone, including the executor's login token, ESO's tokens, and its own.

A scan revokes, by accessor:

- every recorded accessor (either of a `minted` entry's two) whose request has a
  `closed` entry, and every recorded root accessor older than the grace period;
- every unrecorded executor token when no intent is open, and every unrecorded
  executor token older than the grace period.

The grace period is 5 minutes (`openbao_revoker_grace_seconds`), measured from the
token's creation time on the server to the server's own time, which the script takes from
the `Date` header of OpenBao's answer to the listing, never from the pod's clock. A
listing without a usable `Date` header makes the scan incomplete, and it revokes nothing. The executor abandons a request after 2
minutes, so a token older than the grace period has outlived its change.

### The audit record

The revoker reads the record through a read-only mount of the executor's volume
claim (`approval-executor-record`, `openbao_record_pvc_name`). Only the executor
writes the volume. Before it reads anything, the revoker checks the whole chain the
way dmf-init's `verify` does (sequence, `prev`, `hash`, segment order, the restore
and anchor rules). The port cites dmf-init revision `ade3967`.
The writer is `AuditLog` in dmf-init (`src/dmf_init/audit/chain.py`). Two records that it
wrote are in `tests/fixtures/audit-record/`; the revoker's tests parse them, and dmf-init's
`tests/test_audit_revoker_fixture.py` writes them again and compares the bytes, so a
change to the format fails one side until both change.

Both pods mount the same claim and both carry a `nodeSelector` on
`openbao_record_node`, which defaults to the first control host. On the
`local-path` class, which the sandbox lane uses, the provisioner is
`rancher.io/local-path`: it creates the volume as a directory on the node of the
first pod that mounts the claim and pins the volume there with a node affinity on
`kubernetes.io/hostname`. The default class of the other lanes is `longhorn`, which
is not node-local: a ReadWriteOnce volume attaches to one node at a time, and the
`nodeSelector` is what keeps both pods on that node.

### The fail-closed rule

When the record is missing, unreadable, or fails the chain check (a hash mismatch, a
sequence gap, a truncated newest entry), no accessor counts as recorded. The
revoker then revokes only executor tokens older than the grace period, whether an
intent is open or not. It never revokes a younger token, because a request may be
in flight. A scan with an unreadable record logs one line that starts with `ALERT
revoker-record-unreadable` and sets the metric below to 0.

### Trust boundary

The read-only mount stops the revoker from changing the record. It does not make the
record authentic. The chain has no key, so whoever can write the volume or the node
can rewrite the record with matching hashes, and the revoker accepts it. Only the
executor holds write access to the volume. A compromise of the node or of root on it
is outside this control.

### Reading its alert

The pod serves `GET /metrics` on port 9102 and carries the `prometheus.io/scrape`
annotations, so the `kubernetes-pods` job scrapes it.

| Metric | Meaning |
|---|---|
| `dmf_openbao_revoker_record_readable` | 1 when the last scan read a valid record, 0 when it did not |
| `dmf_openbao_revoker_last_scan_timestamp_seconds` | When the last complete scan finished |
| `dmf_openbao_revoker_revoked_total` | Tokens revoked since the pod started |
| `dmf_openbao_revoker_failures` | Steps that failed in the latest scan: the login, the listing, a lookup or a revoke |

A scan is complete only when every step of it succeeded and returned a well-formed answer.
A malformed listing is rejected whole. A failed or malformed step is logged, the scan goes
on to the next token, and the scan then counts as incomplete: it leaves the last-scan time
where it was and raises the failures gauge. A lookup that gets HTTP 400 with the single message `invalid accessor`, or a revoke that gets
HTTP 400 with the single message `token not found`, means that the token is gone, and is not a
failure. OpenBao uses 400 for any invalid request, so every other 400 is a failure: another
message, more than one message, the other endpoint's message, or a body that is
not an OpenBao error list. OpenBao wraps its messages as `N error(s) occurred:` followed by
one `\t* <message>` line each, and the script reads that wrapper before it compares; a wrapper that
does not parse is a failure.

Three rules in `roles/base/prometheus/templates/rules/alerts.yaml.j2` use them:
`OpenBaoRevokerRecordUnreadable` (critical, after 3 minutes),
`OpenBaoRevokerNotScanning` (warning, when no complete scan finishes for 5 minutes, or no
revoker pod reports) and
`OpenBaoRevokerScanFailing` (warning, when the failures gauge stays above zero for 3
minutes). To find the cause, read the log of the revoker pod: the
`ALERT` line gives the reason, and `revoked ACCESSOR: reason` lines show every revoke.
Log lines hold accessors and never a token.

### Install order

The revoker would revoke the installer's own root token, because the record holds no
entry for it. So a fresh init stops the revoker (`revoker-pause.yml`) before `bao operator
init` makes the first token, and waits for its pods to end. A rerun makes no root token and
leaves the revoker running, and applies the Deployment without a replica count so that the
apply keeps it as it is. A new Deployment has zero replicas.

`revoker-resume.yml` sets one replica after the init token and the short write token are
proved revoked, and then waits for the Deployment to report a ready replica. The pod is ready
only after a complete scan, and stops being ready when none finished within three scan
intervals, so the wait ends when the revoker has logged in, listed the tokens and looked
each one up. If that does not happen within `openbao_revoker_ready_retries` times
`openbao_revoker_ready_delay` seconds, the install fails and names the pod to read. The
rescue of `100-openbao.yml` starts the revoker again after it has proved the install's
tokens refused, unless the answer to the mint was lost, because that token cannot be named.

### Seed step

Secrets that need a root token (`secret/platform/*` and the per-app admin paths) are
written during the install, because nothing after it makes a root token. Just before
the role revokes its short write token, it runs `openbao_install_seed_command` on the
operator host (`delegate_to: localhost`), with that token on the first line of stdin
and in no argument. dmf-env's `bin/run-playbook.sh` sets the command to
`bootstrap-secrets.sh seed-install <env>` for the pre-seed wrappers and
`100-openbao.yml`. The role shows the command's messages, and a non-zero status ends
the run: the rescue of `100-openbao.yml` revokes the tokens and proves that the server
refuses them. The seed may then be partial, because it makes several writes, so an
environment whose seed failed is rebuilt. A fresh install with an empty command stops
before init makes any token. A rerun holds no token and seeds nothing.

The write token lives 10 to 15 minutes (`openbao_k8s_write_token_ttl`), counted from the
mint, so the seed shares that time with the install's own writes.

## Root token generation

OpenBao 2.6 and later send `bao operator generate-root` only to the authenticated
`sys/generate-root-token/*` endpoints. The unauthenticated `sys/generate-root/*`
endpoints stay disabled: do not set `disable_unauthed_generate_root_endpoints`
to `false`. The caller therefore needs a token, and a quorum of unseal shares
as before.

- The policy `generate-root` (`files/policies/generate-root.hcl`) grants the
  two paths that the command uses, and nothing else. The role writes it on every
  run. Its name is `openbao_generate_root_policy_name`.
- Only the approval executor's Kubernetes-auth role holds the policy. `ops-admin`
  never does: the install doesn't attach it, so the password and the unseal shares
  together are no second path to a root token. The identity verifier fails when any
  other principal holds the policy.
- The install does every step that needs a root token with its own initial token
  (the init token, and the short token that the role mints from it), and revokes both
  before the revoker starts. See "Install order" above.
- After the install, no playbook, role or script in this repository creates a root
  token. The executor is the only caller, in a later slice.

### No manual procedure

This role has no manual procedure for a root token on an installed environment. If the
executor can't run, the way back is a rebuild: environments installed before this
change are rebuilt, and no step removes the policy from a live environment.
OpenBao's recovery mode gives only a short-lived recovery token for repairs to
storage, never a root token, and its procedure is hazardous. Long-lived sites decide
their own way back before they rely on this model.

Two changes used to need a root token after the install:

- Rotating the `ops-admin` password is the `ops-admin-password-rotation` entry of the
  catalogue, which the executor runs.
- Writing the Kubernetes auth config or a missing standing role on an installed
  environment has no catalogue entry. The installed-environment playbook that did it
  is retired, and an environment that needs the change is rebuilt.

## Catalogue of setup requests

`files/catalogue.yml` lists the setup requests that the approval service offers.
Each entry names a pinned policy file under `files/policies/catalogue/`, holds the
SHA-256 digest of that file, lists the change's `requests` (path and capability),
and states `request_count`, the number of those requests. The header of the file
describes every field. On every run, `tasks/catalogue.yml` writes each pinned
policy to OpenBao under the entry's `policy_name`, and renders the file and the
policy files into the ConfigMap `setup-catalogue` in `openbao_setup_requests_namespace`.

To add an entry:

1. Write the policy file in `files/policies/catalogue/`. Grant `auth/token/revoke-self`
   and only the paths and capabilities that the change uses. A wildcard or a `sys/` path fails the tests.
2. List the requests that the change makes with the token, and set `request_count`
   to their number. Where a request writes parameters, limit them with
   `allowed_parameters` in the policy, and do not grant a path whose update takes
   more than the change needs (for example, the user path of the userpass mount takes
   `token_policies`). Do not count the token revoke: the executor ends the token with
   `auth/token/revoke-self`, which every pinned policy grants and the count leaves
   out. If a parameter changes the count, fix the parameter in the schema.
3. Add the entry to `files/catalogue.yml` and set `policy_sha256` to the output of
   `shasum -a 256 <policy file>`.
4. Run `tests/test_openbao_catalogue.py` with the Python of Ansible. It checks the
   entry's `requests` against the policy.

To change a policy, edit the file and update `policy_sha256` in the same change.

## The identity verifier

`playbooks/verify-openbao-identity-model.yml` checks, among other things, that nothing
in the identity configuration holds power it should not hold. It reads with the
`identity-verifier` policy (`files/policies/identity-verifier.hcl`: read and list of
`sys/auth`, the users, roles, entities and groups, and nothing that returns a secret),
which `policy-reconciler` carries on a fresh install. It logs in as `policy-reconciler`,
which also reads `sys/policies/acl/*`.
The list stanzas of the policy end in a slash, because OpenBao matches a list request with the
slash on its path. `tests/test_openbao_identity_verifier_paths.py` reads every path that the
verifier's tasks request and checks it against the policy.

`bootstrap-verify.yml` runs the playbook.

What it checks:

- **Allowlist.** Every enabled auth mount is in `verify_identity_allowed_auth_mounts`
  (`token/`, `userpass/`, `kubernetes/` and `approle/`: the mounts this role enables, each
  with its type). A mount outside it, or a listed mount of another type, fails the run, and
  the message names the mount and its type. Add a mount to the list in the same change
  that enables it in the role.
- **Holders.** It reads every userpass user, AppRole role, token role, and Kubernetes role
  under those mounts, and every identity entity and group. A principal holds a policy when
  it lists it. A token role holds what it may hand out (`allowed_policies`, and
  `allowed_policies_glob` by prefix). A group also passes its policies to its member groups, to any
  depth, and to its member entities. It fails when:
  - any principal except the Kubernetes role `openbao_executor_k8s_role` on `kubernetes/` holds
    `generate-root`;
  - any principal except the Kubernetes role `openbao_revoker_k8s_role` on `kubernetes/` holds the
    revoker's policy;
  - any principal holds a `catalogue-*` policy. Only the scoped tokens that the executor
    mints carry those.
- **Digests.** For every policy that this role writes from a file (generate-root, the
  revoker's, setup-issuer, the rotation scheduler's, identity-verifier and each catalogue
  policy), the live text must equal the file. Both texts are compared by SHA-256 without
  trailing whitespace, because the role writes the file's text and one final newline.
  The policies that `tasks/main.yml` writes from inline text are not compared.
- **Grants of the generate-root endpoints.** `tasks/verify-generate-root-grants.yml` lists
  every live ACL policy, reads each one except `root` and the executor's own, and fails when
  one has a path stanza that matches `sys/generate-root-token/*` or `sys/generate-root/*` and
  grants any capability but `deny`. It matches an exact path, a `+` segment and a trailing
  `*`, in HCL and in JSON. The failure names the policy and the path. This covers the policies
  that the digest check leaves out. It does not remove a trust: `policy-reconciler` can rewrite
  any ACL policy, the one that lets the verifier read included, so the check is as strong as
  that identity is trusted.

A failure names the principal or the policy. For example,
`userpass user userpass/ops-admin holds generate-root` means that the user's policy list
has `generate-root`: remove it. `generate-root differs from files/policies/generate-root.hcl (live 1a2b..., file 3c4d...)`
means that someone changed the policy in OpenBao, or the file changed and the role has not
run. `token-revoker is missing from OpenBao` means that the role has not written it. The
run also says whether the executor and the revoker roles are installed. Where one is not,
no principal may hold its policy.

A fresh install passes the verifier: `ops-admin` holds no `generate-root` policy.
Only a fresh install attaches `identity-verifier` to `policy-reconciler`. An environment
installed before slice B is rebuilt (ADR-0051), not migrated.

- **Revoker.** `tasks/verify-revoker-running.yml` fails when the revoker Deployment is
  missing, stopped, or has no ready replica. The pod is ready only after a complete scan, so a
  ready replica means that the last complete scan is recent.

A new allowed auth mount needs a branch for its type in "Work out which sources of principals
to read" in `tasks/verify-identity-holders.yml`; a test fails when a type in
`verify_identity_allowed_auth_mounts` has no such branch.

The tests in `tests/test-openbao-identity-verifier.yml` run the verifier against the
fake bao with an identity fixture.

## Upgrade from 2.5.x to 2.7.x

Upstream supports large version jumps and asks you to read the notes of each
version in between. It documents a rolling upgrade as: standbys first, the
active node last, and never a fail over from a newer to an older version. The
fix for request forwarding between nodes of different minor versions is in
2.6.3 and in 2.7.0, so go straight to 2.7.x. Do not stop at 2.6.0 to 2.6.2.

Take a snapshot first. Then, from `dmf-env`:

1. Run `100-openbao.yml` without the override. The StatefulSet gets the new image
   and the listener setting.
   - A single pod restarts by itself, and the role unseals it.
   - A Raft cluster restarts nothing. Run `103-openbao-restart-pod.yml` for one
     pod at a time, standbys first and the leader last, and unseal each pod
     before the next. The restart guard refuses to start while a pod is sealed
     or not a healthy voter.
2. Run `320-cert-manager.yml`. It confirms that each pod serves the current
   certificate.
3. Run `vertical-resilience/120-openbao-raft-snapshot.yml`, so the snapshot job
   runs the same release.

A 2.7 server refuses root token generation from a caller that lacks the policy, and
only the approval executor's role may hold it. A check protects the upgrade: before
the StatefulSet changes to an image of 2.6 or later, the role logs in as
policy-reconciler (password from the break-glass file), reads the
`approval-executor` role, revokes the login token, and stops unless the role lists
the `generate-root` policy. It runs in `tasks/workload.yml`, so `100-openbao.yml` and the grow-HA play are both covered. A new
install, an unchanged image and an image older than 2.6 skip it. An environment
installed before the executor existed has no such role: rebuild it.
