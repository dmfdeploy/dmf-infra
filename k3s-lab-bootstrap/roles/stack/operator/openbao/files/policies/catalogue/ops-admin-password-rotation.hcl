# Pinned policy of the catalogue entry ops-admin-password-rotation. The executor
# mints one token with only this policy and a use limit equal to the entry's
# request_count. The first stanza is the one request of the change, and the
# second ends the token. Nothing else is granted. Change this file only together
# with the digest in files/catalogue.yml.

# Set a new password for ops-admin. The endpoint changes the password and nothing
# else of the user. The user path itself is not granted: an update there takes
# token_policies, which would let the token attach a policy to the user.
# allowed_parameters lets the request carry the password only.
path "auth/userpass/users/ops-admin/password" {
  capabilities = ["update"]
  allowed_parameters = {
    "password" = []
  }
}

# End the token. The token carries no default policy, so every pinned policy grants
# this itself. The executor revokes the token when the change ends. The request is
# not part of request_count.
path "auth/token/revoke-self" {
  capabilities = ["update"]
}
