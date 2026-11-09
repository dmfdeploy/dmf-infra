# The snapshot job reads the Raft snapshot and nothing else.
path "sys/storage/raft/snapshot" {
  capabilities = ["read"]
}
