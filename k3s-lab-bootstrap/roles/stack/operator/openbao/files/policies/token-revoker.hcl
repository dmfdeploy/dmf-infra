# The standing token revoker (files/revoker/revoker.py). It lists the accessors of
# the live tokens, looks each one up, and revokes by accessor. Listing accessors
# needs sudo. The policy grants nothing else.
path "auth/token/accessors" {
  capabilities = ["list", "sudo"]
}

path "auth/token/lookup-accessor" {
  capabilities = ["update"]
}

path "auth/token/revoke-accessor" {
  capabilities = ["update"]
}
