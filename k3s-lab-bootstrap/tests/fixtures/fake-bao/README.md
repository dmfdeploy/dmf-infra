# Fake bao

`bao` in this directory stands in for the OpenBao command-line tool in the playbook tests. The
comment at the top of the script lists every call that it answers and every
`FAKE_*` variable. This file covers the parts that model tokens and identity
configuration for the approval executor and the revoker.

State lives in `$FAKE_STATE`. Each call takes the token from `BAO_TOKEN`.

## Scoped tokens

`bao token create` makes a scoped token when its policies exclude `root`, or when
it sets `-use-limit` or `-explicit-max-ttl`. A scoped token is a file
`$FAKE_STATE/tokens/NAME.json`, named `fake-scoped-N`. Other tokens that `token
create` makes keep their old form: `fake-root-short-N`, a root token that is
recorded in `token-NAME.json`. A call without `-policy` makes one of these: the
child inherits the policies of its root parent, so its answer lists `root` only.

A token that has a stored record in `tokens/` holds the policies of that record
and nothing more. The names `fake-root*`, `fake-ops-token`, `fake-scheduler*`,
`fake-policy*`, `fake-eso*` and `fake-approle-token` grant power only to a token
that has no stored record.

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
| Seeding | A test can write `tokens/NAME.json` with `policies`, `orphan`, `created`, `ttl`, `uses_left` (`null` for no limit) and an optional `accessor`. A token with the `root` policy acts as a root token. |

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

A JWT of the form `fake-sa:NAMESPACE:NAME` must match the role's bound
ServiceAccount and namespace. Any other JWT that is not empty skips that check.
An unknown role and an empty JWT fail.

`read auth/kubernetes/role/NAME` and `list auth/kubernetes/role` need a root token
or the policy in `FAKE_VERIFIER_POLICY` (default `identity-verifier`).

## Generate-root

With `FAKE_GENROOT_AUTH` set, `bao operator generate-root` needs a live token that
is the `ops-admin` login token, a root token, or a scoped token with the
`generate-root` policy.

## Identity reads

These need a root token or the policy in `FAKE_VERIFIER_POLICY`. All answers
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

A call takes `-format=json` or not. The answer is JSON, except for `list` without
the flag, which prints the tool's table. An empty list exits 2, as the real
tool does: `{}` with the flag, "No value found" without it.

## Variables

| Variable | Meaning |
|---|---|
| `FAKE_NOW` | The fake clock, in epoch seconds. |
| `FAKE_REVOKER_POLICY` | The policy for the accessor calls (default `token-revoker`). |
| `FAKE_VERIFIER_POLICY` | The policy for the identity reads (default `identity-verifier`). |

Tests of the fake itself: `tests/test_openbao_fake_bao_tokens.py`.
