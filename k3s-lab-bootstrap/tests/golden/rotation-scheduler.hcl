# Rotation scheduler: runs the rotation transaction for the runtime AppRole targets.
# Rendered from vars/openbao-approle-lifecycle-registry.json by
# lib/approle_lifecycle/scheduler_policy.py. Do not edit by hand.

path "auth/token/revoke-self" {
  capabilities = ["update"]
}

# external-secrets

path "auth/approle/role/external-secrets" {
  capabilities = ["read"]
}

path "auth/approle/role/external-secrets/secret-id/lookup" {
  capabilities = ["update"]
}

path "auth/approle/role/external-secrets/custom-secret-id" {
  capabilities = ["update"]
  required_parameters = ["secret_id", "ttl", "num_uses"]
  allowed_parameters = {
    "secret_id" = []
    "ttl"       = ["2592000s"]
    "num_uses"  = ["0"]
  }
}

path "auth/approle/role/external-secrets/secret-id-accessor/destroy" {
  capabilities = ["update"]
}

path "secret/data/platform/approle-rotation/external-secrets" {
  capabilities = ["create", "read", "update"]
}

path "secret/data/platform/approle-rotation-transactions/external-secrets" {
  capabilities = ["create", "read", "update"]
}

path "secret/data/platform/approle-rotation-staging/external-secrets" {
  capabilities = ["create", "read"]
}

path "secret/metadata/platform/approle-rotation-staging/external-secrets" {
  capabilities = ["delete"]
}

# object-storage-app-backups

path "auth/approle/role/object-storage-app-backups" {
  capabilities = ["read"]
}

path "auth/approle/role/object-storage-app-backups/secret-id/lookup" {
  capabilities = ["update"]
}

path "auth/approle/role/object-storage-app-backups/custom-secret-id" {
  capabilities = ["update"]
  required_parameters = ["secret_id", "ttl", "num_uses"]
  allowed_parameters = {
    "secret_id" = []
    "ttl"       = ["31536000s"]
    "num_uses"  = ["0"]
  }
}

path "auth/approle/role/object-storage-app-backups/secret-id-accessor/destroy" {
  capabilities = ["update"]
}

path "secret/data/platform/approle-rotation/object-storage-app-backups" {
  capabilities = ["create", "read", "update"]
}

path "secret/data/platform/approle-rotation-transactions/object-storage-app-backups" {
  capabilities = ["create", "read", "update"]
}

path "secret/data/platform/approle-rotation-staging/object-storage-app-backups" {
  capabilities = ["create", "read"]
}

path "secret/metadata/platform/approle-rotation-staging/object-storage-app-backups" {
  capabilities = ["delete"]
}

path "secret/data/platform/eso-bindings/object-storage-app-backups" {
  capabilities = ["create", "read", "update"]
}

# object-storage-openbao-snapshots

path "auth/approle/role/object-storage-openbao-snapshots" {
  capabilities = ["read"]
}

path "auth/approle/role/object-storage-openbao-snapshots/secret-id/lookup" {
  capabilities = ["update"]
}

path "auth/approle/role/object-storage-openbao-snapshots/custom-secret-id" {
  capabilities = ["update"]
  required_parameters = ["secret_id", "ttl", "num_uses"]
  allowed_parameters = {
    "secret_id" = []
    "ttl"       = ["31536000s"]
    "num_uses"  = ["0"]
  }
}

path "auth/approle/role/object-storage-openbao-snapshots/secret-id-accessor/destroy" {
  capabilities = ["update"]
}

path "secret/data/platform/approle-rotation/object-storage-openbao-snapshots" {
  capabilities = ["create", "read", "update"]
}

path "secret/data/platform/approle-rotation-transactions/object-storage-openbao-snapshots" {
  capabilities = ["create", "read", "update"]
}

path "secret/data/platform/approle-rotation-staging/object-storage-openbao-snapshots" {
  capabilities = ["create", "read"]
}

path "secret/metadata/platform/approle-rotation-staging/object-storage-openbao-snapshots" {
  capabilities = ["delete"]
}

path "secret/data/platform/eso-bindings/object-storage-openbao-snapshots" {
  capabilities = ["create", "read", "update"]
}
