# Fake bao

`bao` in this directory stands in for the OpenBao command-line tool in the playbook tests. The
comment at the top of the script lists every call that it answers and every
`FAKE_*` variable. This file covers the parts that model tokens and identity
configuration for the approval executor and the revoker.

State lives in `$FAKE_STATE`. Each call takes the token from `BAO_TOKEN`.

## Tokens

Every token that `bao token create` makes, and every token of a Kubernetes login, is
one file in the token store, `$FAKE_STATE/tokens/NAME.json`. One model in the script,
`token_model`, decides whether a token is live, what it may do, how many uses it has
left, which accessor it has and what a revoke takes down. The handlers only ask it. A
revoked token has a file `$FAKE_STATE/revoked-NAME`. A root-policy
token with no `-use-limit` and no `-explicit-max-ttl` is named `fake-root-short-N`.
Any other token is named `fake-scoped-N`. A call without `-policy` makes a
`fake-root-short-N` token: the child inherits the policies of its root parent, so
its answer lists `root` only.

A token that has a stored record in `tokens/` holds the policies of that record
and nothing more. The names `fake-root*`, `fake-ops-token`, `fake-scheduler*`,
`fake-policy*`, `fake-eso*` and `fake-approle-token` are legacy tokens: they grant
power only when no record exists, through one small profile map in the model. A
profile never makes a token live and never skips use counting: a revoked legacy token is
refused like any other.

The fake trusts a policy by name: a token that holds `token-revoker` or `generate-root`
passes the matching calls. The paths and capabilities of each real policy file are checked
by that file's own static test, and a test of the fake checks that every name the fake trusts
maps to a policy file with such a test.

The identity verifier's policy is the exception: the fake reads the policy file and matches
each request against its stanzas with OpenBao's rules (`acl_match.py`). A list request
ends in a slash, so a list stanza without the slash grants nothing, as on a real server.
`FAKE_VERIFIER_POLICY_FILE` names another policy file for a test.

Flags of `token create`: `-policy`, `-orphan`, `-ttl`, `-explicit-max-ttl`,
`-use-limit`, `-renewable`, `-no-default-policy` and `-format`. Only the caller
with a root token may create a token, and a token with a use limit can't create
one, as in OpenBao. Policies are stored in lower case, sorted, and without
repeats. A token with the `root` policy holds only that policy, and other tokens
also get `default` unless `-no-default-policy` is set.

| Behaviour | What the fake does |
|---|---|
| Use limit | Each request that carries the token spends one use. A refused request spends one too. The token is revoked after the request that spends the last use, and that request can't look the token up. |
| Lifetime | The lifetime is `-ttl`, cut to `-explicit-max-ttl` when that is shorter, and to 32 days. Without either, a `root` token never expires and can't be renewed, and other tokens live 32 days. A token past its lifetime, or whose parent's lifetime is over (unless it's an orphan), is refused. |
| Clock | `FAKE_NOW` (epoch seconds, default `1800000000`) is the time of every call. A test moves it to age a token. |
| Seeding | A test can write `tokens/NAME.json` with `policies`, `orphan`, `parent`, `created`, `ttl`, `explicit_max_ttl`, `uses_left`, `renewable` and an optional `accessor`. A token with the `root` policy acts as a root token. A missing or `null` `uses_left`, `ttl` or `explicit_max_ttl` means no limit, and a missing `created` means the time of the call. A `uses_left` of 0 or less means the token is spent: every call refuses it, the accessor list leaves it out, and no lookup finds it. A `uses_left` of 1 allows one request. A token ends at the shorter of `ttl` and `explicit_max_ttl`. A negative lifetime, or a `created` after `FAKE_NOW`, makes the token not live. A missing `orphan` means the token is an orphan when it has no `parent`. A token also ends with any ancestor that is revoked, spent or past its lifetime, at any depth, unless it's an orphan. A `parent` with no record is a legacy token when its name starts with `fake-`; any other missing parent, or a cycle of parents, makes the token not live. |

## Accessor calls

These need a root token or a token with the policy in `FAKE_REVOKER_POLICY`
(default `token-revoker`). The fake checks the policy by name.

- `list auth/token/accessors` lists the accessors of live tokens: the stored
  tokens and the tokens that `token create` recorded. The accessor of a token
  is `NAME-accessor` unless the seed file sets one.
- `write auth/token/lookup-accessor accessor=A` returns `policies`,
  `creation_time`, `creation_ttl`, `ttl` (what is left), `num_uses` (what is
  left), `orphan` and `renewable`. A revoked or expired token has no accessor.
- `write auth/token/revoke-accessor accessor=A` revokes the token and every
  token below it that was made without `-orphan`, at any depth. Orphans survive.
  An unknown or already revoked accessor gives a warning and exit status 0.

## Kubernetes login

`write auth/kubernetes/login role=NAME jwt=-` reads the JWT from standard input.
`jwt=VALUE` also works. The call needs no token. It returns an orphan token with
the policies of the role, which a test sets with a `write auth/kubernetes/role/NAME`
call or by writing `k8s-auth/role-NAME.json`. The token lives for the role's
`token_ttl`, cut to `token_max_ttl`.

The JWT must have the form `fake-sa:NAMESPACE:NAME` and match the role's bound
ServiceAccount and namespace (`*` matches any). Any other JWT is refused with 403, as a
bad credential is. An unknown role and an empty JWT fail.

`read auth/kubernetes/role/NAME` and `list auth/kubernetes/role` need a root token
or the policy in `FAKE_VERIFIER_POLICY` (default `identity-verifier`).

## Capabilities

`token capabilities PATH` answers for the caller. A token with a record holding `root`
gets `["root"]`. Any other token with a record gets `["deny"]`. A legacy token gets
`["read"]`, and a `fake-eso*` legacy token gets `["read"]` on `secret/*`, `FAKE_ESO_PKI_GRANT`
on `pki/*` and `["deny"]` elsewhere. A test can set `FAKE_CAPS_JSON` to answer for every
other path and `FAKE_CAPS_MISSING` to deny listed paths.

## Generate-root

With `FAKE_GENROOT_AUTH` set, `bao operator generate-root` needs a live token that
is the `ops-admin` login token, a root token, or a scoped token with the
`generate-root` policy.

## Policies

`bao write sys/policies/acl/NAME` stores the text in `$FAKE_STATE/policies/NAME`, `bao read
sys/policies/acl/NAME` returns it, and `bao list sys/policies/acl` lists the files of that
directory. All three need a root token, or any live token when `FAKE_POLICY_WRITER` is set.

## Identity reads

These need a root token or a token that holds the policy in `FAKE_VERIFIER_POLICY`, and
the policy file must grant the read or the list (`FAKE_VERIFIER_POLICY_FILE`). All answers
come from `$FAKE_STATE/identity.json`:

```json
{
  "auth_methods": {"token/": {"type": "token"}, "kubernetes/": {"type": "kubernetes"}},
  "userpass_users": {"ops-admin": {"token_policies": ["app-admin-writer"]}},
  "approle_roles": {"external-secrets": {"token_policies": ["eso-reader"]}},
  "token_roles": {"batch": {"allowed_policies": ["a"]}},
  "entities": {"ID": {"name": "alice", "policies": ["p"]}},
  "groups": {"ID": {"name": "ops", "policies": ["p"], "member_group_ids": ["ID"]}}
}
```

| Call | Answer |
|---|---|
| `read sys/auth` | `auth_methods`, as the response of the server has it. Without a fixture: the token method. |
| `list auth/userpass/users`, `read auth/userpass/users/NAME` | `userpass_users`. When the fixture file exists, a user that it does not name has no value (exit 2). Without the file, `ops-admin` is the only user: the list holds it and the read is the record the other tests use. |
| `list auth/approle/role`, `read auth/approle/role/NAME` | `approle_roles`. This includes `external-secrets`: with the fixture file, a missing role has no value (exit 2). |
| `list auth/token/roles`, `read auth/token/roles/NAME` | `token_roles` |
| `list identity/entity/id`, `read identity/entity/id/ID` | `entities`, with `id` added |
| `list identity/group/id`, `read identity/group/id/ID` | `groups`, with `id` added |

A further Kubernetes mount (a mount in `auth_methods` that is not `kubernetes/`) keeps its roles
as files, `$FAKE_STATE/k8s-auth-MOUNT/role-NAME.json`, where MOUNT has no trailing slash. `list
auth/MOUNT/role` and `read auth/MOUNT/role/NAME` answer from them, for a root token or the
verifier policy. The identity test uses this to put a role named like the executor's on a second
mount. These calls are outside the table above because they don't read `identity.json`.

A call takes `-format=json` or not. The answer is JSON, except for `list` without
the flag, which prints the tool's table. An empty list exits 2, as the real
tool does: `{}` with the flag, "No value found" without it.

## Variables

| Variable | Meaning |
|---|---|
| `FAKE_NOW` | The fake clock, in epoch seconds. |
| `FAKE_REVOKER_POLICY` | The policy for the accessor calls (default `token-revoker`). |
| `FAKE_VERIFIER_POLICY` | The policy for the identity reads (default `identity-verifier`). |

## Not modelled

- A policy grants its power to any token that holds its name. The fake doesn't read the rules in it.
- A token that is not root can't create tokens, and the child policies are not checked
  against the parent's, a role's or the `default` rules.
- Kubernetes login doesn't verify a JWT's signature, issuer or audience, and sets the
  TTL and policies from the role only.
- A lookup is an approximation: it doesn't model renewal, and it reports `num_uses` as
  0 for a token with no limit.
- An ancestor that ends takes its descendants down at once. OpenBao revokes them as its
  expiry manager runs.
- `-no-default-policy`, policy case and the other policy rules are a small subset of the
  server's.

Tests of the fake itself: `tests/test_openbao_fake_bao_tokens.py`.
