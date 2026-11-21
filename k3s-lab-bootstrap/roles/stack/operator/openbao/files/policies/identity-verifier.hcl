# The identity verifier (playbooks/verify-openbao-identity-model.yml). It reads the
# configuration of the auth methods and of the identity store, and the text of the
# policies, and writes nothing. It reads no secret: the paths below hold names and
# policy lists only. Each read path ends in the single-segment wildcard `+`, so a
# role's role ID and SecretID paths, one segment deeper, are not covered.
#
# A list request reaches OpenBao with a trailing slash (`bao list auth/token/roles` asks
# for `auth/token/roles/`), and the slash is part of the path that a stanza must match.
# So each list stanza below ends in a slash, and a stanza without it would grant nothing.
path "sys/auth" {
  capabilities = ["read"]
}

path "auth/+/users/" {
  capabilities = ["list"]
}

path "auth/+/users/+" {
  capabilities = ["read"]
}

path "auth/+/role/" {
  capabilities = ["list"]
}

path "auth/+/role/+" {
  capabilities = ["read"]
}

path "auth/token/roles/" {
  capabilities = ["list"]
}

path "auth/token/roles/+" {
  capabilities = ["read"]
}

path "identity/entity/id/" {
  capabilities = ["list"]
}

path "identity/entity/id/+" {
  capabilities = ["read"]
}

path "identity/group/id/" {
  capabilities = ["list"]
}

path "identity/group/id/+" {
  capabilities = ["read"]
}
