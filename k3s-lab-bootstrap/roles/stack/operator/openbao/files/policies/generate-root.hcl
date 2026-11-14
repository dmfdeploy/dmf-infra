# Root token generation as a caller with a quorum of unseal shares. OpenBao 2.6
# and later send `bao operator generate-root` only to these two authenticated
# endpoints. `attempt` reads the progress, starts an attempt (an update) and
# cancels one (a delete). `update` takes each unseal share. The OTP and the
# decode of the result run in the client and need no path.
path "sys/generate-root-token/attempt" {
  capabilities = ["read", "update", "delete"]
}

path "sys/generate-root-token/update" {
  capabilities = ["update"]
}
