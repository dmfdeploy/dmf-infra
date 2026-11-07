# Setup-only issuer: mints, looks up and destroys SecretIDs for one AppRole.
# Plan step A.2: the create accepts only ttl=1h and num_uses=1.
path "auth/approle/role/born-inventory-netbox-reader/secret-id" {
  capabilities        = ["update"]
  required_parameters = ["ttl", "num_uses"]
  allowed_parameters = {
    "ttl"      = ["1h"]
    "num_uses" = ["1"]
  }
}

path "auth/approle/role/born-inventory-netbox-reader/secret-id-accessor/lookup" {
  capabilities = ["update"]
}

path "auth/approle/role/born-inventory-netbox-reader/secret-id-accessor/destroy" {
  capabilities = ["update"]
}

path "auth/token/revoke-self" {
  capabilities = ["update"]
}
