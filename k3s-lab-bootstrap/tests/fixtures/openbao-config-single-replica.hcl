storage "raft" {
  path    = "/openbao/data"
  node_id = "1"
}

listener "tcp" {
  address         = "0.0.0.0:8200"
  cluster_address = "0.0.0.0:8201"
  tls_disable     = 0
  tls_cert_file   = "/openbao/config/tls/tls.crt"
  tls_key_file    = "/openbao/config/tls/tls.key"
}

api_addr      = "https://openbao.openbao.svc.cluster.local:8200"
cluster_addr  = "https://openbao.openbao.svc.cluster.local:8201"
disable_mlock = true
ui            = true
