# cert-manager

**Scope:** TLS certificate issuance for the dmfdeploy platform.

- Installs cert-manager via Helm (Jetstack repo)
- Creates a Let's Encrypt ClusterIssuer (HTTP-01 via Traefik)
- Creates a default Traefik Certificate resource for the configured SAN set
- Supports ACME (Let's Encrypt), a CA for the OpenBao listener (below), or self-signed fallback

**Status:** Implemented.

## OpenBao certificate authority

OpenBao TLS is internal, so the role builds its own CA in the OpenBao
namespace. It does not use ACME and does not depend on the site's TLS mode.

1. A self-signed `Issuer` (`cert_manager_openbao_issuer_name`) signs the CA
   certificate once.
2. A root `Certificate` (`cert_manager_openbao_ca_certificate_name`) holds the
   CA in the Secret `cert_manager_openbao_ca_secret_name`. The root lasts ten
   years, renews a year early, and keeps its private key when it renews.
3. A CA `Issuer` (`cert_manager_openbao_ca_issuer_name`) signs the OpenBao
   `Certificate`. Its Secret (`cert_manager_openbao_tls_secret_name`) keeps its
   name and holds `ca.crt` beside `tls.crt` and `tls.key`.

Clients trust the CA, never the leaf, so a leaf renewal changes nothing that
they trust.

After each issuance the role proves that the CA issued the certificate: the
Secret names the CA `Issuer` as its issuer, `tls.crt` is not the CA
certificate, and the certificate verifies against the CA's own Secret. A
`ca.crt` that is present proves nothing, because a self-signed certificate from
before the move can hold a `ca.crt` that equals itself.

Then the role reloads the listener certificate of each running OpenBao pod. It
waits until the pod holds the Secret's files, sends `SIGHUP` to the server, and
compares the certificate the pod serves on its own address with the projected
one. A pod that does not match stops the play. The reload does nothing when no
pod runs, as on a fresh install. For the procedure to move an environment or to
renew, see the umbrella runbook `docs/runbooks/openbao-listener-certificate.md`.

| Variable | Default | Description |
|---|---|---|
| `cert_manager_openbao_ca_certificate_name` | `openbao-ca` | Root `Certificate` of the CA |
| `cert_manager_openbao_ca_secret_name` | `openbao-ca-tls` | Secret that holds the root |
| `cert_manager_openbao_ca_issuer_name` | `openbao-ca` | CA `Issuer` that signs the leaf |
| `cert_manager_openbao_ca_common_name` | `openbao-ca` | Common name of the root |
| `cert_manager_openbao_ca_duration` | `87600h` | Lifetime of the root |
| `cert_manager_openbao_ca_renew_before` | `8760h` | How long before expiry the root renews |
| `cert_manager_openbao_reload_enabled` | `true` | Reload the listener certificate after each issuance |
| `cert_manager_openbao_reload_retries` / `_delay` | `12` / `5` | Tries and seconds between tries for the reload check |
| `cert_manager_openbao_projection_retries` / `_delay` | `30` / `10` | Tries and seconds between tries for the kubelet to project the Secret into the pod |

Set these env vars to activate ACME issuance:

- `cert_manager_acme_email`
- `cert_manager_dns_names`

Keep `cert_manager_cluster_domain` as the primary apex hostname when you want a
stable top-level domain reference and the legacy default secret naming.
