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
standby, and a sealed pod has none):

```bash
for p in openbao-0 openbao-1 openbao-2; do
  kubectl -n openbao exec "$p" -- bao status -format=json | jq -e '.is_self == true' >/dev/null && echo "$p"
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

   Play 106 owns the root token from here: it revokes the token in an `always` section
   and proves that the server refuses it, also after a failure.
9. Check that the token is gone, then clear the variables.

   ```bash
   bao_as "$root" token lookup </dev/null; echo "exit status $?"   # must not be 0
   unset root encoded otp login_token
   ```

   If the token is still valid (exit status 0), or 106 ended with `UNREACHABLE` or said
   that the revoke failed, revoke it by its accessor in Recovery with `$root_accessor`.

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
