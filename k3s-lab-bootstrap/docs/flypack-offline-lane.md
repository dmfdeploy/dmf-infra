# Flypack Offline Lane — Canonical Spec

Canonical implementation spec for the **flypack** deployment profile, a.k.a. the
"OB truck" lane. This document supersedes partial specs in `DMF Platform Plan.md`
(see [Supersedes](#supersedes)) and defines the contract the bootstrap code must
honour for this profile.

Status: **draft for implementation**, 2026-04-18.

## Purpose

A flypack is a **single, self-contained k3s appliance** delivered to a client,
commissioned once, and then operated **indefinitely without ever re-contacting
the factory or any central service**. The hardware, software, identity store,
secret store, TLS material, and image registry all live on the truck.

Primary operational model:

```
factory (build + commission) → ship → client site (live forever)
```

There is no "dock", no "redock", no wg2 back to central, no phone-home, no
automatic update channel. Updates are delivered by an operator as **signed
physical media** (USB drive) and applied with `truckctl`.

## Non-goals

This lane explicitly does **not** cover:

- Connected flypacks with periodic central sync. That model is retained only
  as an optional future extension; it is not the primary case and is not
  required for feature completeness.
- Cross-truck federation or synchronization. Each truck is a sovereign island.
- Pulling NetBox inventory or AWX playbooks from a central hub at runtime.
- Any form of automatic update, remote management, or outbound telemetry.
- Truly air-gapped *sites* in the DMF Platform Plan §276–279 sense (indefinite
  isolation with no physical operator visits). The flypack lane assumes an
  operator visits at least once per year to apply an update pack; anything
  beyond that is a different product.

## Supersedes

This document supersedes the following claims from `DMF Platform Plan.md`:

| Superseded claim | Replaced by |
|---|---|
| §254–257 — ESO caches central secrets and refetches on reconnect | [Secrets delivery](#secrets-delivery) — ESO sources from the truck's own embedded OpenBao; no central to refetch from |
| §270–274 — "log in once at dock" oauth2-proxy session model (14–30 days) | [Identity](#identity) — embedded Authentik is the truck's own IdP; no central session to depend on |
| §276–279 — slim per-flypack Authentik only if isolated >30 days | Embedded Authentik is the **default** for this lane, not an exception |
| §298–300 — `standalone-ephemeral` and `standalone-scalable` sub-profiles | [Profile](#profile) — single tier "flypack" with composable role toggles; sub-profiles deferred until a second client demands a different shape |

## Profile

Single tier: **`flypack`**. No sub-profiles at this time.

All functional variation is expressed as **role-level toggles** in the profile
manifest, e.g.:

```yaml
flypack:
  nodes: 1                    # or 3 for HA variants
  storage: local-path         # or longhorn (replica 1)
  ingress_mode: nodeport-only # or metallb-l2 on controlled LAN
  tls_mode: factory-acme      # or customer-provided
  roles:
    openbao: true             # always true for this lane
    authentik: true           # always true for this lane
    zot: true                 # always true for this lane
    netbox: false             # default off for flypack (see non-goals)
    awx: false                # default off for flypack
    forgejo: false            # default off for flypack
    grafana: true
    prometheus: true
    loki: true
```

Future tiers (`flypack-sm`, `flypack-md`, …) become preset toggle combinations.
No code change required to introduce them.

## Per-truck root of trust

The truck is the root of trust for **everything on the truck**. There is no
external authority it trusts for runtime decisions, with the single exception
of the factory code-signing chain used to verify update packs (see
[Update packs](#update-packs)).

Three components together form this root of trust:

1. **Embedded OpenBao** — secret store, Kubernetes auth, AppRole issuer,
   private PKI.
2. **Embedded Authentik** — OIDC IdP for all human-facing apps on the truck.
3. **Per-truck private CA** (rooted in OpenBao PKI) — issues certs for
   cluster-internal service-to-service TLS. Not exposed to client devices.

### Embedded OpenBao

- Deployed in-cluster as a `StatefulSet` with a PVC (storage class per
  `storage` toggle).
- Integrated Storage (Raft). Single node at `nodes: 1`; 3-member Raft when
  `nodes: 3`.
- **Sealed on every cold boot.** Operator performs the unseal ritual
  (see [Unseal ritual](#unseal-ritual)).
- Auth methods enabled at commissioning: `approle` (for ESO and automation),
  `kubernetes` (for in-cluster workload identities), `userpass` (break-glass
  admin login).
- Does **not** enable any auth method requiring external network reach
  (no OIDC-auth-from-Authentik, because Authentik itself boots after OpenBao
  is unsealed).

#### Unseal ritual

- **Shamir 3-of-5.**
- **All 5 shares are handed to the client.** We retain no copies. This is a
  deliberate choice for client sovereignty.
- **Loss of ≥3 shares is unrecoverable.** The truck must be rebuilt from
  factory. This is documented prominently in the client handover kit.
- `truckctl unseal` prompts for 3 shares interactively, pipes them to
  `vault operator unseal`, and verifies `Sealed: false` before exiting.
- Expected cold-boot cadence: rare (planned maintenance, power loss). The
  ritual is **not** a failure mode, it is how the truck starts.

### Embedded Authentik

- Single-replica (or 2-replica when `nodes: 3`) Deployment with its own
  Postgres and Redis inside the truck.
- **Database and Redis credentials come from OpenBao** via ESO. Authentik
  cannot come up until OpenBao is unsealed and ESO has materialized its
  secrets.
- **Blueprint-seeded at factory.** The blueprint set (checked into the
  bootstrap repo) defines:
  - Default groups: `ops-admin`, `ops-operator`, `viewer`, `break-glass`.
  - Authentication flows: password + TOTP (required), WebAuthn (optional).
  - Recovery flow (email-less; uses admin-assisted reset via `ops-admin`).
  - OIDC provider definitions for every app on the truck (Grafana,
    Prometheus, Loki, and any enabled optional role).
- Factory bakes **one superadmin** (local creds in OpenBao) and **one
  break-glass local user** (creds on a sealed envelope to the client).
  Real client users are created on-site by the client's ops staff.
- **No federation** to any external IdP. Authentik trusts only itself.

### Per-truck private CA

- Issued by OpenBao `pki` secrets engine at commissioning, intermediate
  signed under a per-truck root.
- Used **only for cluster-internal traffic**: pod-to-pod mTLS, internal
  service endpoints, Authentik ↔ Postgres, etc.
- Never presented to client devices. Client devices only see the
  public-facing certificate (see [Public-facing TLS](#public-facing-tls)).
- Long-lived (10-year root, 5-year intermediate). Renewal happens via
  update pack before expiry.

## Identity

All human-facing applications on the truck authenticate via **embedded
Authentik over OIDC**. There is no oauth2-proxy and no session-cached
dock-login model in this lane.

- Apps with native OIDC support (Grafana, Forgejo if enabled, AWX if enabled)
  configure Authentik as their OIDC provider via ESO-materialized client
  secrets.
- Apps without native OIDC sit behind an ingress-layer OIDC middleware
  (Traefik `ForwardAuth` against a small `oauth2-proxy` pointed at
  *embedded* Authentik, not central).
- Token lifetime is a normal OIDC flow: access token minutes, refresh token
  hours/days. Since both sides are on the truck, refresh is always reachable.
- Break-glass path: the `break-glass` local user in Authentik and the
  `userpass` admin in OpenBao are both usable without OIDC working. Credentials
  are in the sealed client envelope.

## Public-facing TLS

The truck's public-facing HTTPS endpoints (the Authentik UI, app UIs) present
a certificate the **client's own devices must trust without installing a
root**.

This is a commissioning-time pluggable choice. Two modes:

### `tls_mode: factory-acme` (default)

- Per-truck FQDN under a domain we own: `<truck-id>.truck.<operator-domain>`
  (configurable).
- Wildcard certificate `*.<truck-id>.truck.<operator-domain>` issued by a
  **commercial ACME provider with 1-year validity** (ZeroSSL primary,
  Sectigo or Buypass as alternatives). Let's Encrypt (90-day) is the
  fallback if commercial ACME is unavailable.
- **Validation uses DNS-01** against DNS records we control. The truck is
  **not** required to participate in validation at any point.
- **Renewal happens at the factory.** We run an automated renewal job per
  shipped truck against our DNS zone, produce an update pack containing the
  renewed cert, and dispatch it to the operator well before expiry.
- **Expiry behaviour if no renewal lands in time:** cluster-internal
  traffic keeps working (private CA is separate); client browsers get cert
  warnings on the public UIs. Truck is degraded, not dead. Operator applies
  the overdue update pack to recover.

### `tls_mode: customer-provided`

For clients who require their own PKI (enterprise internal CA, their own
commercial cert for a customer-branded hostname, or a cert from their
corporate certificate authority).

- Client supplies at commissioning: **cert PEM, private key, full chain**.
- Ingress configuration uses the client-supplied hostname; DNS for that
  hostname is the client's responsibility.
- Factory ACME path is **not** provisioned in this mode — the factory
  renewal pipeline is skipped for this truck.
- **Renewal is the client's responsibility.** They hand new cert material
  to the operator when the existing cert nears expiry. Operator installs it:

  ```
  truckctl install-cert \
      --cert /media/usb/new.pem \
      --key  /media/usb/new.key \
      --chain /media/usb/chain.pem
  ```

  This command is a standalone operation; it does **not** require the full
  update-pack signing chain because the cert material comes from the client
  directly. It writes to the ingress TLS secret atomically and triggers a
  graceful reload.
- `truckctl status` surfaces cert expiry date for monitoring by the client's
  ops staff.

### Dual mode

Not supported in v1. If a client requires both a public-internet-facing cert
for external viewers and a customer-CA-signed cert for internal staff, they
deploy two separate trucks or accept that only one vhost is protected.
Revisit if a client actually requires it.

## Image registry

- **Embedded Zot** deployed as a Deployment with PVC, exposed as an internal
  ClusterIP service.
- `containerd` on each node is configured with Zot as the **exclusive**
  image mirror. No external registry is reachable at runtime.
- Factory build process produces an **image lockfile** per profile
  (`flypack/images.lock`) pinning every referenced image by
  `registry/name@sha256:<digest>`. The lockfile is generated from the
  rendered manifests, committed to the bootstrap repo, and used as the
  authoritative list for `zot sync` at factory bake time.
- Any image not in the lockfile will fail to pull on the truck. This is
  intentional — drift-proofs the image set.
- Update packs carry image deltas (only the digests not already present
  on the truck).

## Update packs

The single channel for **all** changes to a shipped truck after handover:
cert renewals, application upgrades, manifest changes, Authentik blueprint
updates, image additions, OpenBao policy changes.

### Format

A signed tarball:

```
pack-<truck-id>-<iso-date>-<short-sha>.tar.sig
├── manifest.yaml             # declares what this pack contains
├── images/                   # OCI layout, only delta digests
├── helm-values/              # values overrides per release
├── manifests/                # raw Kubernetes manifests
├── secrets/                  # ESO ClusterSecretStore targets (references
│                             # OpenBao paths, no plaintext material)
├── tls/                      # factory-acme mode only: renewed cert + chain
├── authentik/                # blueprint deltas
├── hooks/                    # pre/post apply scripts
└── sig/                      # detached signature over manifest.yaml digest
```

### Signing

- **Long-lived offline factory root** (RSA 4096 or Ed25519). Public key baked
  into every truck at commissioning; never updated after ship.
- **Shorter-lived factory signing intermediate**, signed by root. Rotated
  annually or on compromise. New intermediates are shipped in-band inside
  update packs (the current intermediate signs the new intermediate, chain
  of trust unbroken).
- `truckctl apply-update` verifies the signature against the baked-in root.
  Unsigned or invalid packs are refused.

### Operator workflow

```
1. Operator plugs USB drive into truck
2. truckctl apply-update /media/usb/pack-<id>.tar.sig
3. truckctl prompts: "Pack claims to <summary from manifest>. Apply? [y/N]"
4. Signature verified → hooks/pre run → images imported to Zot →
   manifests/helm applied → hooks/post run
5. truckctl prints a summary + any failed post-hooks
6. Operator runs truckctl status to confirm health
```

### Cadence

- **Minimum: once per year** (cert renewal for `factory-acme` mode).
- **Recommended: quarterly** (security patches, image updates).
- **On-demand** for feature changes or critical fixes.

### Customer-provided TLS exception

In `tls_mode: customer-provided`, cert renewal does **not** go through the
update pack mechanism (see `truckctl install-cert` above). All other update
types still do.

## Secrets delivery

- **External Secrets Operator (ESO)** is a base-fixed component (per
  Platform Plan §632).
- Its `ClusterSecretStore` for the flypack lane points at
  `https://openbao.openbao.svc.cluster.local:8200` — the truck's own
  embedded OpenBao.
- Every application consumes secrets via `ExternalSecret` objects that
  reference paths in the truck's OpenBao.
- **Factory seed**: the commissioning playbook writes all required secrets
  (Authentik DB creds, ESO AppRole, signing keys, app API keys, etc.) to
  OpenBao **after** OpenBao is initialized and before apps are deployed.
- **In-field rotation**: via update pack (for factory-managed secrets) or
  via the OpenBao UI/CLI by authorized operators (for client-managed
  secrets, if any).

## Backup and restore

All backup is **local only**. No cloud bucket, no off-truck destination
unless the client explicitly provides one (NAS, external drive).

### Backup contents

1. **OpenBao Raft snapshot** (encrypted with the client's public backup
   key, provided at commissioning).
2. **Authentik Postgres dump** (encrypted same way).
3. **Application PVCs** (Velero-style snapshot or raw `tar` depending on
   storage class).
4. **Manifest of truck state** (versions, image digests, cert expiry,
   Authentik blueprint hash) for diagnostic purposes.

### Backup workflow

```
truckctl backup /media/ext-disk/backups/
```

- Runs Ansible playbook that drives each component's native backup path.
- Output is a single timestamped, encrypted archive.
- Exit code non-zero if any component's backup failed; partial archives
  are rejected.

### Restore workflow

```
truckctl restore /media/ext-disk/backups/<archive>
```

- **Requires Shamir unseal material** (OpenBao restore needs it).
- Documented as a planned procedure with a runbook; not automated end to
  end because partial-state recovery is too varied.

## Operator tooling: `truckctl`

Single CLI entry point for all field operations. Distributed as a static
binary (or thin Python/Go wrapper) preinstalled on the truck at
commissioning.

### Commands

| Command | Purpose |
|---|---|
| `truckctl unseal` | Prompt for Shamir keys, unseal OpenBao, verify |
| `truckctl seal` | Emergency seal (before unauthorized physical access) |
| `truckctl status` | Health check: pods, OpenBao seal state, cert expiry, disk, backup age |
| `truckctl apply-update <pack>` | Verify signature, apply signed update pack |
| `truckctl install-cert --cert ... --key ... --chain ...` | Install customer-provided TLS material (customer-provided mode only) |
| `truckctl backup <dest>` | Encrypted backup to local destination |
| `truckctl restore <archive>` | Restore from backup (Shamir required) |
| `truckctl logs <component>` | Tail logs from a named component |
| `truckctl diag` | Produce a diagnostic bundle for factory support |

Under the hood each command is an Ansible playbook invocation plus
`kubectl`/`vault` calls. Operators never need to run Ansible directly.

### Authentication for `truckctl`

- Runs as a specific local Unix user on the truck's control-plane node.
- Read-only commands (`status`, `logs`, `diag`) require only that user.
- Write commands (`apply-update`, `install-cert`, `backup`, `restore`,
  `unseal`) require a second factor: either the break-glass OpenBao
  userpass password or a physical hardware token (future).

## Bootstrap order

### Factory build (per truck)

1. **Hardware commissioning** — install Debian/k3s image, set `<truck-id>`,
   assign hostname, apply per-truck IDs.
2. **k3s up** — single- or three-node cluster, storage class provisioned.
3. **OpenBao deploy** — StatefulSet, init, Shamir 5-key generation, record
   keys, **print keys** for sealed-envelope handover. Unseal immediately
   for factory seeding.
4. **OpenBao configure** — enable PKI, issue truck's private CA,
   enable `approle`/`kubernetes`/`userpass`, create policies.
5. **Seed base secrets** — write all app credentials, signing keys, ESO
   AppRole.
6. **ESO deploy** — ClusterSecretStore points at embedded OpenBao, auth
   via AppRole.
7. **Zot deploy** — empty registry, then sync images per
   `flypack/images.lock`. Configure containerd mirror.
8. **Authentik deploy** — Postgres + Redis + server + worker. DB secret
   from ESO. Apply blueprint set. Seed superadmin + break-glass.
9. **Ingress + public TLS**:
   - `factory-acme`: request cert, install on ingress.
   - `customer-provided`: wait for client cert material, install.
10. **Deploy remaining stack** (monitoring, enabled optional roles).
11. **Bake update-pack signing trust** (copy factory root public key to
    `/etc/truckctl/trust/root.pub`).
12. **Smoke test** — `truckctl status` green, Authentik login works, all
    OIDC-integrated apps reachable.
13. **Produce handover kit** — sealed Shamir envelopes, break-glass creds,
    client backup public key (if customer provides one), runbook.
14. **Seal OpenBao before ship.**

### Field bring-up (per truck, at client site)

1. Unbox, rack, power.
2. Connect LAN (per client network plan). No WAN required.
3. Boot.
4. `truckctl unseal` with 3 of 5 Shamir keys.
5. `truckctl status` → all green.
6. Operator hands over break-glass envelope, client backup key storage,
   runbook.
7. Client creates real users in Authentik.
8. Client tests end-to-end workflow.

### Field update (per operator visit)

1. Operator arrives with signed update pack on USB.
2. `truckctl status` → baseline health.
3. `truckctl apply-update /media/usb/pack-...`.
4. `truckctl status` → post-update health.
5. If customer-provided TLS cert is overdue, `truckctl install-cert ...`
   separately.
6. `truckctl backup /media/ext/backup-<date>` as departure snapshot.

## Implementation contract for bootstrap code

The `k3s-lab-bootstrap` roles must honour the following to qualify as
flypack-compliant:

- No role may assume outbound internet access at runtime. All external
  references (images, charts, ACME endpoints, OIDC IdP URLs) must either
  be local or be explicitly gated behind `flypack_connected: false` (always
  false in this lane).
- No role may hardcode a central service URL. Every `ClusterSecretStore`,
  OIDC issuer, registry, and backup destination must be profile-parameterised.
- Every role must produce a bring-up result readable by `truckctl status`
  (health endpoint or a known file in `/var/lib/truckctl/health/<role>`).
- Every role that owns persistent state must provide a `backup` and
  `restore` subtask invokable from `truckctl backup`/`truckctl restore`.
- No role may panic or degrade silently on missing optional connectivity.
  Any feature that requires WAN must fail closed and emit a clear
  diagnostic, not retry forever.

## Open items / future work

Explicitly deferred, not part of this spec's v1:

- `truckctl` implementation (currently conceptual).
- Factory build runbook (step-by-step with commands, versions, expected
  output). To be authored once the first end-to-end factory bake runs.
- Field bring-up runbook (client-site procedures).
- Update pack exact binary format and signer tooling.
- Cert expiry monitoring at factory (automated job per shipped truck).
- Optional "connected flypack" extension (wg2 to central for opt-in
  telemetry/log forwarding).
- Multi-truck fleet management (if we ever have >5 shipped trucks).
- Hardware token second factor for `truckctl` write commands.
- Client-side monitoring export (if a client wants to integrate truck
  alerts into their existing monitoring).

## Decision log

| Date | Decision | Rationale |
|---|---|---|
| 2026-04-18 | Airgapped flypack is the **primary** lane, not an edge case | User requirement: trucks ship and never reconnect |
| 2026-04-18 | Embedded OpenBao, not central OpenBao over wg2 | No runtime network dependency on our infra |
| 2026-04-18 | Embedded Authentik, not central with oauth2-proxy sessions | Session refresh requires central reach; we have no central reach |
| 2026-04-18 | Shamir 3-of-5 unseal, **all 5 keys to client** | Client sovereignty; we keep no copies |
| 2026-04-18 | Per-truck FQDN under our domain for `factory-acme` mode | DNS-01 renewal without truck touching internet |
| 2026-04-18 | 1-year commercial ACME cert as default | Matches "operator visit once a year" cadence |
| 2026-04-18 | `customer-provided` TLS mode as first-class alternative | Client requirement for enterprise CA support |
| 2026-04-18 | Signed update pack as single change channel | Verifiable, offline-deliverable, uniform workflow |
| 2026-04-18 | Single tier `flypack`, composable role toggles | Avoid premature tiering; split later if justified |
| 2026-04-18 | No NetBox/AWX/Forgejo on flypack by default | Out of scope; central-side concerns |
