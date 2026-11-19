"""Check the token lifecycle that the fake bao recorded for a play that trades a
generate-root token for a short-lived one: the installed-environment play
(roles/stack/operator/openbao/tasks/kubernetes-auth-existing.yml) and the ops-admin
rotation (playbooks/vertical-orchestration/120-ops-admin-rotation.yml). A fresh install
of the same role (playbooks/vertical-security/100-openbao.yml) trades its init token
the same way; this text calls the token that is traded the generate-root token.

The fake bao keeps $FAKE_STATE/bao-calls.log ("TOKEN ARGS" per call, in order),
tokens/NAME.json for each token that `token create` made, and revoked-NAME for each
revoked token. For one generate-root token, this module lists what is wrong:

  - the generate-root token made a call other than the policy lookup, the mint, the
    revoke and the lookup that proves the revoke (and the reads that root_reads names),
    or made them out of order: it must mint at most once, only after the policy lookup
    and before its first revoke, look itself up after a revoke only to prove that
    revoke, and make no read after its first revoke,
  - the number of tokens minted from it differs from what the run should mint,
  - a minted token is not an orphan, lacks the root policy, is renewable, or has a
    lifetime outside 10 to 15 minutes,
  - a write (a call under one of the write prefixes, auth/kubernetes/ by default) ran with
    a token that is not one of the minted tokens,
  - a write that follows the first call of the generate-root token ran before a revoke
    of it that worked and a later lookup that the server refused (the fake logs those
    outcomes as "TOKEN =revoked" and "TOKEN =refused"; when the run revoked it),
  - a minted token that the run should have revoked is still valid, or one that the run
    should have left (a run that ended UNREACHABLE) was revoked.

Run as a script: token_lifecycle.py STATE_DIR ROOT_TOKEN MINTED ROOT_REVOKED SHORT_REVOKED
where MINTED is a count and the last two are yes or no. It prints the problems as JSON.
A caller of problems() names the calls that count as writes with write_prefixes, and the
reads that the generate-root token may make before its revoke with root_reads.
"""
import json
import os
import re
import sys

MIN_TTL = 600
MAX_TTL = 900
ROOT_CALLS = re.compile(r"^token (lookup|create|revoke) ")
KUBERNETES_WRITES = ("write auth/kubernetes/",)


def problems(state, root_token, minted_expected, root_revoked, short_revoked, write_prefixes=KUBERNETES_WRITES, root_reads=()):
    found = []
    calls = []
    path = os.path.join(state, "bao-calls.log")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            for line in handle.read().splitlines():
                token, _, args = line.partition(" ")
                calls.append((token, args))
    revoked = {name[len("revoked-"):] for name in os.listdir(state) if name.startswith("revoked-")}
    minted = {}
    store = os.path.join(state, "tokens")
    for name in sorted(os.listdir(store) if os.path.isdir(store) else []):
        match = re.fullmatch(r"(.+)\.json", name)
        if match:
            with open(os.path.join(store, name), encoding="utf-8") as handle:
                record = json.load(handle)
            if record["parent"] == root_token:
                minted[match.group(1)] = record

    root_ops = []
    for token, args in calls:
        if token == root_token and not args.startswith("="):
            match = ROOT_CALLS.match(args)
            if match:
                root_ops.append(match.group(1))
            elif root_reads and args.startswith(tuple(root_reads)):
                root_ops.append("read")
            else:
                found.append("the generate-root token made the call: " + args)
    if "create" in root_ops and "lookup" not in root_ops[:root_ops.index("create")]:
        found.append("the generate-root token minted a token before the policy lookup")
    if root_ops.count("create") > 1:
        found.append("the generate-root token minted more than one token")
    first_revoke = root_ops.index("revoke") if "revoke" in root_ops else len(root_ops)
    if "create" in root_ops[first_revoke:]:
        found.append("the generate-root token minted a token after its revoke")
    if "read" in root_ops[first_revoke:]:
        found.append("the generate-root token made a read after its revoke")
    for i in range(first_revoke, len(root_ops)):
        if root_ops[i] == "lookup" and root_ops[i - 1] != "revoke":
            found.append("the generate-root token was looked up after its revoke without a revoke just before")

    if len(minted) != minted_expected:
        found.append("expected %d minted tokens, found %d" % (minted_expected, len(minted)))
    for name, record in minted.items():
        if not record["orphan"]:
            found.append(name + " is not an orphan")
        if "root" not in record["policies"]:
            found.append(name + " lacks the root policy")
        if record.get("renewable", True):
            found.append(name + " is renewable")
        if not MIN_TTL <= record["ttl"] <= MAX_TTL:
            found.append("%s has a TTL of %s seconds, outside %d to %d" % (name, record["ttl"], MIN_TTL, MAX_TTL))
        if (name in revoked) != (short_revoked == "yes"):
            found.append(name + (" is revoked" if name in revoked else " is not revoked") + ", expected the opposite")
    if (root_token in revoked) != (root_revoked == "yes"):
        found.append("the generate-root token is " + ("revoked" if root_token in revoked else "not revoked") + ", expected the opposite")

    first = next((i for i, (token, _) in enumerate(calls) if token == root_token), None)
    if first is not None:
        for i, (token, args) in enumerate(calls[first:], start=first):
            if args.startswith(tuple(write_prefixes)):
                if token not in minted:
                    found.append("a write ran with a token that was not minted: " + args)
                if root_revoked == "yes":
                    outcomes = [a for t, a in calls[:i] if t == root_token and a.startswith("=")]
                    proved = "=revoked" in outcomes and "=refused" in outcomes[outcomes.index("=revoked") + 1:]
                    if not proved:
                        found.append("a write ran before the generate-root token was revoked and proved refused: " + args)
    return found


if __name__ == "__main__":
    state, root_token, minted_expected, root_revoked, short_revoked = sys.argv[1:6]
    print(json.dumps(problems(state, root_token, int(minted_expected), root_revoked, short_revoked)))
