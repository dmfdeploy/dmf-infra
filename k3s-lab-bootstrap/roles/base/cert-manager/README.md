# cert-manager

**Scope:** TLS certificate issuance for the DMF platform.

- Installs cert-manager via Helm (Jetstack repo)
- Creates a Let's Encrypt ClusterIssuer (HTTP-01 via Traefik)
- Creates a default Traefik Certificate resource for the configured SAN set
- Supports ACME (Let's Encrypt), OpenBao PKI, or self-signed fallback

**Status:** Implemented.

Set these env vars to activate ACME issuance:

- `cert_manager_acme_email`
- `cert_manager_dns_names`

Keep `cert_manager_cluster_domain` as the primary apex hostname when you want a
stable top-level domain reference and the legacy default secret naming.
