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
environment gets the missing ones from
`playbooks/vertical-security/106-openbao-kubernetes-auth.yml`.

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
and keeps no token between scans, so it stores no token on disk. A lookup that fails,
or that has no `policies` list, is logged and the token is left alone.

An *executor token* is a token whose policies include `root` or any policy named in
`files/catalogue.yml` (all start with `catalogue-`). The revoker leaves every
other token alone, including the executor's login token, ESO's tokens, and its own.

A scan revokes, by accessor:

- every recorded accessor (either of a `minted` entry's two) whose request has a
  `closed` entry, and every recorded accessor older than the grace period;
- every unrecorded executor token when no intent is open, and every unrecorded
  executor token older than the grace period.

The grace period is 5 minutes (`openbao_revoker_grace_seconds`), measured from the
token's creation time on the server. The executor abandons a request after 2
minutes, so a token older than the grace period has outlived its change.

### The audit record

The revoker reads the record through a read-only mount of the executor's volume
claim (`approval-executor-record`, `openbao_record_pvc_name`). Only the executor
writes the volume. Before it reads anything, the revoker checks the whole chain the
way dmf-init's `verify` does (sequence, `prev`, `hash`, segment order, the restore
and anchor rules). The port cites dmf-init revision `ade3967`.

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
| `dmf_openbao_revoker_last_scan_timestamp_seconds` | When the last scan that logged in finished |
| `dmf_openbao_revoker_revoked_total` | Tokens revoked since the pod started |
| `dmf_openbao_revoker_failures` | Failed lists, lookups, and revokes in the last scan |

Two rules in `roles/base/prometheus/templates/rules/alerts.yaml.j2` use them:
`OpenBaoRevokerRecordUnreadable` (critical, after 3 minutes) and
`OpenBaoRevokerNotScanning` (warning, when no scan finishes for 5 minutes). To find
the cause, read the log of the revoker pod: the `ALERT` line gives the reason, and
`revoked ACCESSOR: reason` lines show every revoke. Log lines hold accessors and
never a token.

### Install order

The revoker would revoke the installer's own root token, because the record holds no
entry for it. So `main.yml` stops the revoker (`revoker-pause.yml`) before the first
task that can create or use a root-policy token, and waits for its pods to end. The
Deployment is applied with zero replicas. Only `revoker-resume.yml` sets one replica,
after the install has finished and the init token and the short write token are
proved revoked. A fresh install with `openbao_dispose_root_token: false` keeps the
init token and leaves the revoker stopped. `106-openbao-kubernetes-auth.yml` stops
and starts it the same way around its own tokens.

## Root token generation

OpenBao 2.6 and later send `bao operator generate-root` only to the authenticated
`sys/generate-root-token/*` endpoints. The unauthenticated `sys/generate-root/*`
endpoints stay disabled: do not set `disable_unauthed_generate_root_endpoints`
to `false`. The caller therefore needs a token, and a quorum of unseal shares
as before.

- The policy `generate-root` (`files/policies/generate-root.hcl`) grants the
  two paths that the command uses, and nothing else. The role writes it on every
  run. Its name is `openbao_generate_root_policy_name`.
- `ops-admin` carries the policy next to `app-admin-writer` and
  `app-runtime-writer`. A new install attaches it when it creates the user. On an
  installed environment, run `playbooks/vertical-orchestration/120-ops-admin-rotation.yml`
  with `-e ops_admin_rotation_attach_only=true`. The play sets the policies of the
  user through the policies endpoint of the userpass mount, with a temporary root
  token, and writes no password. The break-glass file stays as it is, so no offline
  copy goes stale. Without the variable the play also rotates the password. Only a
  root token can write `auth/userpass/users/*`.
- Every caller logs in as `ops-admin`, passes the login token to
  `bao operator generate-root` on stdin, and revokes it when it ends, after a
  failure too. The callers are the rotation play and `acquire_temp_root` in
  `dmf-env/bin/bootstrap-secrets.sh`. On 2.5.x the command ignores the token and
  uses the legacy endpoints, so the callers work before and after the upgrade.
- A caller without the credentials of `ops-admin` cannot generate a root token.
  That case needs OpenBao recovery mode, which this role does not cover yet.

### Root token procedure (2.6 and later)

Use this to get a temporary root token on a 2.6 or later server and to give it to
`106-openbao-kubernetes-auth.yml`. You need the break-glass file (the `ops_admin_*`
keys and the unseal shares), OpenBao unsealed, and `ops-admin` holding the
`generate-root` policy. Run the steps one by one in one shell and check each result
before you go on. This is not a script: no step runs by itself, and the shell keeps
the tokens in variables.

**What stays off the command line.** No token, OTP, share or password appears in a
local, SSH or `kubectl` argument: each goes to the pod on stdin. One value is
visible for a short time: `bao operator generate-root` takes the OTP only as a flag,
so while that command runs, the OTP is in the arguments of the `bao` process inside
the pod. It is single use and worthless without the encoded token. Never write a
token, OTP or share to a file.

**The rule.** If any step after you submit the last share fails, or you are unsure
whether it worked, stop and go to Recovery. Do not submit the share again.

Find the active pod first. A standby answers the legacy generate-root endpoint of OpenBao
2.5.2 with a 500, so every call below must go to the active pod. `bao status` needs no
token and prints `"is_self": true` on the active node only (the field is left out on a
standby, and a sealed pod has none). The loop asks every pod with the OpenBao label, so it
covers any replica count:

```bash
for p in $(kubectl -n openbao get pods -l app.kubernetes.io/name=openbao -o name); do
  kubectl -n openbao exec "$p" -- bao status -format=json | jq -e '.is_self == true' >/dev/null && echo "${p#pod/}"
done
```

Check: exactly one pod name is printed. If none or two are, stop: unseal every pod and
wait until one pod leads.

Set up once. `K` reaches that active pod, for example `kubectl -n openbao exec -i openbao-1 --`,
or its form over ssh. `BG` is the break-glass file.

```bash
BG=<the break-glass file>
K="kubectl -n openbao exec -i <the active pod> --"
# Token on the first line of stdin, more stdin after it.
bao_as() { local t=$1; shift; { printf '%s\n' "$t"; cat; } | $K sh -c \
  'IFS= read -r BAO_TOKEN; export BAO_ADDR=https://127.0.0.1:8200 BAO_TOKEN; exec bao "$@"' bao "$@"; }
# Token and OTP on the first two lines of stdin, more stdin after them.
bao_otp() { local t=$1 o=$2; shift 2; { printf '%s\n%s\n' "$t" "$o"; cat; } | $K sh -c \
  'IFS= read -r BAO_TOKEN; IFS= read -r OTP; export BAO_ADDR=https://127.0.0.1:8200 BAO_TOKEN; exec bao operator generate-root "$@" -otp="$OTP"' bao "$@"; }
```

1. Log in as ops-admin. The password is the whole of stdin (`password=-`).

   ```bash
   login=$(jq -j .ops_admin_password "$BG" | $K bao write -format=json auth/userpass/login/ops-admin password=-)
   login_token=$(jq -r .auth.client_token <<<"$login"); login_accessor=$(jq -r .auth.accessor <<<"$login")
   ```

   Check: `login_token` and `login_accessor` are not empty. Keep the accessor.
2. Generate the OTP, then start the attempt.

   ```bash
   otp=$(bao_as "$login_token" operator generate-root -generate-otp -format=json </dev/null | jq -r .otp)
   nonce=$(bao_otp "$login_token" "$otp" -init -format=json </dev/null | jq -r .nonce)
   ```

   Check: `otp` and `nonce` are not empty. If a step fails here, cancel with
   `bao_as "$login_token" operator generate-root -cancel </dev/null`, revoke the login
   token (step 7), and start again. No token exists yet.
3. Submit every share except the last, one call each. Repeat with `i` set to 0, 1, and so
   on, up to the threshold minus two. With a threshold of 1 there is nothing to do here.

   ```bash
   jq -j ".unseal_keys_hex[$i]" "$BG" | bao_as "$login_token" operator generate-root -nonce="$nonce" -format=json -
   ```

   Check: each call prints `"complete": false`. A failure here means no token exists:
   cancel as in step 2, revoke the login token, and start again.
4. Submit the last share. **From here on, the rule applies.**

   ```bash
   out=$(jq -j ".unseal_keys_hex[<threshold minus 1>]" "$BG" | bao_as "$login_token" operator generate-root -nonce="$nonce" -format=json -)
   encoded=$(jq -r '.encoded_token // empty' <<<"$out")
   ```

   Check: `encoded` is not empty. If the call failed, or `encoded` is empty, a token may
   exist: go to Recovery.
5. Decode the token with the OTP.

   ```bash
   root=$(printf '%s' "$encoded" | bao_otp "$login_token" "$otp" -decode=- -format=json | jq -r .token)
   ```

   Check: `root` is not empty. If it is, go to Recovery.
6. Note the accessor of the root token.

   ```bash
   root_accessor=$(bao_as "$root" token lookup -format=json </dev/null | jq -r .data.accessor)
   ```

   Check: `root_accessor` is not empty. If it is, still go on, and use Recovery to find
   the accessor later.
7. Revoke the login token at once.

   ```bash
   bao_as "$login_token" token revoke -self </dev/null
   ```

   Check: exit status 0. If it fails, try again twice. If it still fails, note
   `login_accessor` and revoke the token by its accessor in Recovery.
8. Hand the root token to 106 in its environment variable, for that command only.

   ```bash
   OPENBAO_TEMP_ROOT_TOKEN="$root" bin/run-playbook.sh <env> <path to>/106-openbao-kubernetes-auth.yml
   ```

   Play 106 owns the root token from here. It mints a short-lived orphan token with the
   root policy, revokes the generate-root token before the first write, writes with the
   short token, and revokes the short token in an `always` section, also after a failure.
   The server refuses each revoked token, and the play proves it.
9. Check that the token is gone, then clear the variables.

   ```bash
   bao_as "$root" token lookup </dev/null; echo "exit status $?"   # must not be 0
   unset root encoded otp login_token
   ```

   If the token is still valid (exit status 0), or 106 said that the revoke failed,
   revoke it by its accessor in Recovery with `$root_accessor`. When 106 ended with
   `UNREACHABLE`, the same check applies: the token is usually gone already, because 106
   revokes it before its first write, and the short-lived token that 106 minted expires on
   its own within 15 minutes.

#### Recovery

Use this when a step after the last share fails, when you are unsure whether a root
token was issued, or when a token may still be valid. Work from a new session: do not
reuse the old tokens.

1. Log in again as ops-admin (step 1), generate a new temporary root token with the
   quorum (steps 2 to 5), and call it `new_root`.
2. List the token accessors: `bao_as "$new_root" list auth/token/accessors </dev/null`.
3. Look each accessor up and find the root tokens that you did not just create, and the
   old ops-admin login token (`login_accessor`, if you noted it):
   `bao_as "$new_root" token lookup -accessor <accessor> </dev/null`.
4. Revoke the orphan root token and the old login token:
   `bao_as "$new_root" token revoke -accessor <accessor> </dev/null`.
5. Revoke the new tokens: `bao_as "$new_root" token revoke -self </dev/null`, and the new
   login token the same way.

`120-ops-admin-rotation.yml` and `acquire_temp_root` in `dmf-env` run steps 1 to 5 for
themselves, keep the root token in memory, and revoke it and the login token when they
end. They retry a failed revoke and then stop with the accessor, and they warn when a
root token may exist. They do not hand the token to another play.

`120-ops-admin-rotation.yml` also trades the generate-root token at once, as 106 does: it
mints a short-lived orphan token with the root policy, revokes the generate-root token
before the first write, and writes with the short token. When 120 ends with `UNREACHABLE`,
the header of the play says what can be left: after the revoke only the short token, which
expires on its own within 15 minutes, and before the revoke the generate-root token, which
you revoke by its accessor.

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

## Upgrade from 2.5.x to 2.7.x

Upstream supports large version jumps and asks you to read the notes of each
version in between. It documents a rolling upgrade as: standbys first, the
active node last, and never a fail over from a newer to an older version. The
fix for request forwarding between nodes of different minor versions is in
2.6.3 and in 2.7.0, so go straight to 2.7.x. Do not stop at 2.6.0 to 2.6.2.

Take a snapshot first. Then, from `dmf-env`:

1. Run `100-openbao.yml` with the image that runs now
   (`-e openbao_image=openbao/openbao:2.5.2`). It writes the `generate-root`
   policy and changes no pod.
2. Run `120-ops-admin-rotation.yml -e ops_admin_rotation_attach_only=true` once.
   It attaches the policy to `ops-admin` and keeps the password. On 2.5.x the
   command needs no policy to run.
3. Run `100-openbao.yml` without the override. The StatefulSet gets the new image
   and the listener setting.
   - A single pod restarts by itself, and the role unseals it.
   - A Raft cluster restarts nothing. Run `103-openbao-restart-pod.yml` for one
     pod at a time, standbys first and the leader last, and unseal each pod
     before the next. The restart guard refuses to start while a pod is sealed
     or not a healthy voter.
4. Run `320-cert-manager.yml`. It confirms that each pod serves the current
   certificate.
5. Run `vertical-resilience/120-openbao-raft-snapshot.yml`, so the snapshot job
   runs the same release.

Steps 1 and 2 come first because a 2.7 server refuses root token generation from
a caller that lacks the policy, and a pod that already runs 2.7 cannot give
`120-ops-admin-rotation.yml` that policy.

Both plays revoke every token they issue when they end, after a failure too.

A check protects this order: before the StatefulSet changes to an image of 2.6 or
later, the role logs in as ops-admin (password from the break-glass file), reads the
policies of the login token, revokes it, and stops unless the `generate-root` policy
is there. It runs in `tasks/workload.yml`, so `100-openbao.yml`,
`106-openbao-kubernetes-auth.yml` and the grow-HA play are all covered. A new
install, an unchanged image and an image older than 2.6 skip it.
