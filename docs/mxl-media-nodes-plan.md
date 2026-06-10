# Plan: Aliyun MXL media nodes joined to the existing Hetzner cluster

Status: DRAFT for review · Branch: `feat/mxl-single-node-spike` (must NOT merge to `main`)
Date: 2026-05-30

---

## AS-BUILT (executed + verified live 2026-05-30)

**Outcome: SUCCESS.** Two Aliyun Ubuntu 24.04 ARM nodes (`aliyun-media-01` tailnet
`<media01-tailnet>`, `-02` `<media02-tailnet>`) joined the live g2r6-foa9 Hetzner cluster as k3s
**agents** over Tailscale — `Ready`, `v1.30.6+k3s1`, tainted `dmf.io/mxl=true:NoSchedule`
+ labeled `dmf.io/role=mxl-processor`, INTERNAL-IP = tailnet IP. Cross-cloud pod
networking verified end-to-end (pod→Hetzner CoreDNS pod 0% loss; cluster DNS; TLS to
apiserver ClusterIP).

Rollout (operator-run): `dmf-env/bin/mxl-media-init-creds.sh` (hidden secret entry) →
`bin/tf-apply.sh aliyun-media apply` → `bin/mxl-media-join.sh` (NOT `run-playbook.sh`
— aliyun-media has no OpenBao bundle).

### Ubuntu / cross-cloud divergences (all parameterized w/ Debian-preserving defaults)
| Symptom | Fix (shared playbook param ↔ aliyun-media group_var) |
|---|---|
| SSH host-key verify fail (fresh nodes) | `ANSIBLE_HOST_KEY_CHECKING=False` in `mxl-media-join.sh` |
| `k3s_control_node` undefined (agent-only inv) | `k3s_control_node: g2r6-foa9-node-01` (only used in a `when`) |
| `kubernetes` pip vs apt PyYAML (PEP 668) | `200-baseline` `extra_args` param → `--break-system-packages --ignore-installed` |
| harden `Restart sshd` (Ubuntu unit is `ssh`) | handler `name` param → `harden_sshd_service: ssh` |
| tailscale role ingress debug needs domain | `cert_manager_cluster_domain: media.invalid` |
| **Tailscale × Aliyun CGNAT** (100.100.x DNS/mirrors inside 100.64/10 dropped by tailscale anti-spoof nft) | tailscale `up` param → `tailscale_extra_up_args: --netfilter-mode=off` |

### Cross-cloud reachability (the `10.0.0.x` problem)
Hetzner nodes advertise their flannel VXLAN endpoint as their *private* `10.0.0.x`,
unreachable from Aliyun. **Solved by per-node `/32` tailscale routes** (NOT subnet-router
forwarding, which the live cluster's stacked nftables — kube-router+kube-proxy+tailscale+
harden — blocked): each Hetzner node advertises only its own `10.0.0.x/32`
(`tailscale set --advertise-routes`) + `headscale nodes approve-routes`; traffic then goes
direct to each node, local delivery, no forwarding. Persisted via
`tailscale_advertise_routes: ["{{ k3s_node_ip }}/32"]` in g2r6-foa9 group_vars (mirror to
the umbrella checkout). Return path needs nothing — Aliyun flannel endpoints are tailnet IPs.

### NetworkUnavailable taint
Set by g2r6-foa9's Hetzner CCM route controller, which won't clear it for non-Hetzner
nodes. The media nodes don't need Hetzner CCM routing (flannel-over-tailscale is enough),
so **pods just tolerate `node.kubernetes.io/network-unavailable`** (baked into the
mxl-fabrics-demo chart). Clearing it wouldn't stick (CCM owns it).

### MTU (blocker #1) — VERIFIED 2026-05-30
Aliyun `flannel.1=1230` (auto-derived from tailscale0, correct), Hetzner
`flannel.1=1400` (sized for its 1450 private net). **Validation gate #2 PASSED:**
DF ping from an Aliyun pod caps at 1230 (1300 fails, as expected); a 166KB bulk
Hetzner→Aliyun fetch (node-exporter `/metrics`) completed clean at full speed —
MSS clamps to Aliyun's 1230, so cross-cloud **TCP is fine**. Only large cross-cloud
UDP could fragment. Optional clean fix = cluster-wide flannel MTU 1230 (deferred).
Note: there is no `--flannel-mtu` flag and nothing sets a `*_flannel_mtu` var —
the 1230 is auto-derived by k3s from `--flannel-iface=tailscale0`.

### Firewall
No changes needed — Hetzner harden INPUT `policy accept` + `iifname tailscale0 accept`;
Aliyun `netfilter-mode=off` + SG allows tailscale UDP 41641 + all intra-VSwitch.

---

## 1. Goal

Stand up **two Aliyun ARM nodes** as **MXL media nodes** and join them to the
**existing live Hetzner k3s cluster `g2r6-foa9`** over Tailscale, then deploy the
MXL fabrics spike on them. Hetzner ARM capacity for fresh nodes is unavailable,
so Aliyun (Frankfurt) is the substitute. The whole effort is an additive,
feat-branch-only spike.

Two-step rollout the operator asked for:
1. **Bring the nodes into the cluster** — provision + baseline + harden +
   tailscale + k3s-join + media designation, mirroring vanilla bootstrap as
   closely as possible.
2. **Apply the MXL spike** — the `mxl-hello` (shared-mem baseline) and
   `mxl-fabrics-demo` (cross-host, tcp provider) charts already drafted.

## 2. Design principle: mirror vanilla `main` bootstrap as much as possible

Reuse the existing numbered playbooks unchanged, driven by inventory vars;
add the media nodes as a normal-but-tainted agent group. Keep new code to a
minimum and confined to new files on the feat branch (no edits to shared roles
that would have to land in `main`).

### 2.1 Per-stage mirror analysis

| Stage | Vanilla playbook | Reuse verdict |
|---|---|---|
| Provision | `terraform/aliyun*` (alicloud module exists) | **Reuse** — `cluster_size: 2`, agent role |
| Baseline | `200-baseline.yml` | **Reuse, but Debian-tuned** (see §4 OS) |
| Harden | `210-harden.yml` + `roles/base/harden` | **Reuse**; nftables must allow tailscale + fabrics ports |
| Tailscale | `321-tailscale.yml` | **Reuse as-is** (ephemeral authkey from OpenBao) |
| k3s join | `300-k3s.yml` | **Cannot mirror unchanged** — see §3 |
| Media designation | (none) | **New** — no node-label/taint var hook exists |

## 3. The one unavoidable divergence — cross-cloud k3s join

`300-k3s.yml` assumes the control plane is built in the **same run**:
- join token is read off the freshly-created control node
  (`/var/lib/rancher/k3s/server/node-token`, registered as `k3s_node_token`,
  consumed by agents at L219 via `hostvars[groups['k3s_control'][0]]`);
- `k3s_server_url` is hard-coded from the control node's **private**
  `k3s_node_ip` (L174) → `https://10.0.0.x:6443`.

For our media nodes that is wrong on both counts: the g2r6-foa9 control plane
**already exists** and is reachable from Aliyun **only at its Tailscale IP**
`<ctl-tailnet-ip>`, not the Hetzner-private `10.0.0.x`.

### 3.1 Two candidate approaches (KEY REVIEW QUESTION)

**Approach A — extend the g2r6-foa9 inventory with an Aliyun agent group.**
Add the two nodes to `inventories/g2r6-foa9/` as a `[k3s_agent]`/media group;
run vanilla `300-k3s.yml` against the `g2r6-foa9` env. The existing control
nodes are already in `groups['k3s_control']` (idempotent no-op), the token is
read off live node-01, the agents join.
- Override needed: `k3s_server_url` → `https://<ctl-tailnet-ip>:6443` and
  `k3s_flannel_iface: tailscale0` for the media group only (group_vars).
- Pro: maximal vanilla reuse, real token flow, one cluster inventory.
- Con: mixes two clouds in one env inventory; touches the g2r6-foa9 inventory
  (still feat-branch-only, but it is the "production" testlab inventory).

**Approach B — separate `aliyun-media` env + thin join step.**
Keep an independent `aliyun-media` inventory; a small new play sources the
existing cluster's node-token (via OpenBao / one-time read) and runs the k3s
agent install with `K3S_URL=https://<ctl-tailnet-ip>:6443`.
- Pro: clean separation, g2r6-foa9 inventory untouched, self-contained spike.
- Con: re-implements ~15 lines of the agent-join that vanilla already does;
  must source the token out-of-band.

**DECIDED: Approach B** (qwen architectural review, 2026-05-30). A "papers over"
the structural mismatch but doesn't solve it; concrete footguns with A:
- `300-k3s.yml`'s second play targets `k3s:!k3s_control` → would hit **any
  existing Hetzner agents**, not just the new Aliyun ones, plus re-run
  audit-logging drop-ins on the **live** control nodes (needless churn).
- `200-baseline.yml` is `hosts: all` → would **re-baseline the Hetzner nodes**.
- `tf-render-inventory.sh` regenerates `inventories/g2r6-foa9/hosts.ini` from
  **tofu state**; the Aliyun group entries aren't in g2r6-foa9 state, so a
  re-render silently **wipes** them. Hard footgun.
- g2r6-foa9 is the **live testlab** (Authentik/Forgejo/Grafana/AWX/NetBox/Zot/
  Prometheus). Mixing in Aliyun agents makes group-wide plays hang/fail if an
  Aliyun node is unreachable — a permanent operational tax (§Q5).

B is self-contained and tear-out-able. The token-sourcing "con" is cheap:
`bootstrap-secrets.sh` already has `inventory_control_host()` +
`inventory_host_var()` (SSH-reads a value off the control node) to reuse — the
join play is ~15 lines.

## 4. OS choice — Ubuntu 24.04 (DECIDED 2026-05-30)

**Decision: Ubuntu 24.04 host.** Faithful to the upstream MXL host environment
(and keeps the door open for later bare-process / eRDMA experiments). Guiding
principle from the operator: *mirror vanilla, but where we must diverge for
Ubuntu, we diverge deliberately.*

Known divergence cost to absorb: `200-baseline.yml` is titled "Debian 12 k3s
nodes" and has Debian-specific handling (externally-managed Python/pip — note
Ubuntu 24.04 is *also* externally-managed, so that specific task likely carries
over). The Ubuntu compat pass covers `200-baseline` + `roles/base/harden`
(apt/nftables specifics). Divergences are implemented via **OS-conditional
overrides in media-group vars / a thin addon task**, not by rewriting the shared
Debian playbooks — so `main` stays untouched. Any divergence that genuinely
cannot be expressed as an override is flagged explicitly before it is written.

## 5. Two-plane networking (unchanged from prior design)

- **Cluster plane** (membership, pods, kube-api): over `tailscale0`,
  cross-cloud to Hetzner. Flannel VXLAN encapsulated inside Tailscale WireGuard
  (1280 MTU) → **MTU must be clamped**; this is the #1 validation risk.
- **Fabrics plane** (media-node ↔ media-node): over the shared Aliyun VPC
  private interface `eth1` at full 1500 MTU. `mxl-fabrics-demo` pods run
  `hostNetwork: true` and advertise their eth1 IP → bypass flannel entirely.
- Aliyun security group + host nftables must allow: Tailscale (UDP 41641 + DERP
  443) and the fabrics tcp ports between the two media nodes on the VSwitch CIDR.

## 5a. Pre-flight BLOCKERS (from qwen review — fix before the rollout runs)

These are configuration, not architecture — but each will hard-fail if skipped.
All live in `aliyun-media` group_vars / the join play, so `main` stays clean.

1. **MTU (blackhole risk).** Flannel VXLAN over Tailscale's 1280 MTU leaves
   ~1230 for pods; flannel's auto-clamp over a virtual tunnel iface is
   unreliable. Pin a low flannel MTU and verify with a large-payload transfer
   *before* trusting the cluster plane.
   ⚠️ *Correction to the review:* the exact flags qwen suggested
   (`--flannel-mtu`, `--flannel-kube-subnet-mode=ipvs`) are **not real k3s
   flags**. k3s normally derives flannel MTU from the `--flannel-iface` MTU
   (tailscale0=1280 → 1230). Real mechanisms to verify/choose from: (a) rely on
   k3s deriving 1230 from tailscale0 and test; (b) lower `tailscale0` MTU; or
   (c) drop a custom flannel `net-conf.json`. **Verify empirically — do not ship
   a fictional flag.**
2. **nftables CIDR (nodes firewalled).** `roles/base/harden/templates/nftables.conf.j2`
   accepts a single `harden_private_cidr` = `<hetzner-priv-cidr>` (Hetzner). The Aliyun
   VPC private range won't match → the **fabrics plane on eth1 is dropped**.
   Override `harden_private_cidr` (or add an extra-CIDR list) for the
   aliyun-media group to include the Aliyun VSwitch CIDR. Blocking before
   `210-harden`.
3. **SSH allow-list / ordering (lockout).** `harden_ssh_allow_ipv4` is the
   Hetzner public IP; the Aliyun nodes need the operator's workstation/Tailscale
   IP added for the aliyun-media group — or run harden **after** Tailscale so
   SSH survives via the tailnet. Blocking before `210-harden`.
4. **Aliyun security group + fabrics port.** Stateful SG needs an inbound rule
   for the fabrics tcp port between the two media nodes' eth1 IPs. **Pin the
   port** (the demo's `--service`, e.g. `1234`) in the chart and open exactly
   that in the SG.

## 6. Media designation

No node-label/taint var hook in `300-k3s.yml`. Apply post-join via a new addon
task (off main): label `dmf.io/role=mxl-processor` + taint
`dmf.io/mxl=true:NoSchedule` — mirroring the keys g2r6-foa9's manifest declares
(but never materialized). `mxl-*` charts get matching nodeSelector + toleration.

## 6a. Inventory, storage & monitoring

**Inventory.** `terraform/aliyun-media` renders `inventories/aliyun-media/hosts.ini`
(a `[mxl_media]` group: public IP for bootstrap SSH + `aliyun_private_ip` = eth1
fabrics IP) and `group_vars/all/tofu_outputs.yml`. `k3s_node_ip` is **not** in
hosts.ini — the join play sets it to each node's Tailscale IP at runtime. The
nodes are a **separate env inventory** (Approach B), not added to g2r6-foa9's.

**Storage — no Longhorn.** MXL is tmpfs/shared-memory (demo `-d <tmpfs>`), so
pods use `emptyDir{medium: Memory}`/hostPath, not PVCs. The `NoSchedule` taint
also keeps the Longhorn manager DaemonSet off (it tolerates no extra taints), so
nothing to disable. Also dropped vs a normal env: Cloudflare DNS, SLB/CCM,
object storage, apps.

**Monitoring — the EXISTING g2r6-foa9 Prometheus scrapes them.** No new
Prometheus/Grafana/Loki on these nodes. Once joined:
- kubelet + cAdvisor metrics are auto-scraped via Kubernetes SD (over the tailnet);
- **node-exporter must tolerate the media taint** to land here —
  kube-prometheus-stack's node-exporter defaults to `tolerations: operator
  Exists`, so it should schedule. **VERIFY against the live values**, and add a
  toleration if not.
- Cross-cloud scrape traffic (Prometheus on Hetzner → 100.64.x:9100/10250 on
  Aliyun) rides the tailnet — depends on the §5a MTU fix being correct.

### 7a. NetBox born-inventory registration — DECIDED: SKIP for now
Read of `roles/common/dmf-born-inventory`: the role is **add/update-only** (every
object is "look up → create when count==0"; no `state: absent`, no prune). Adds
are idempotent and **scoped to the media env's own NetBox cluster/site** — they
do not touch g2r6-foa9's records (the role even states "media environments must
live in separate inventory planes"). So registering is safe and not hard; the
only cost is there is **no automated removal** (teardown = manually delete the 2
VM objects, or a small bespoke decommission snippet).

**Decision (operator): skip for now.** Prometheus monitors the nodes via k8s SD
regardless of NetBox; born-inventory only adds AWX-inventory/asset/blackbox
visibility, not core metrics. `netbox_registration: false`. Re-runnable later
with a single `694-born-inventory.yml` against the aliyun-media env if wanted.

## 7. Credential mechanic (Aliyun AccessKey entry)

Reuse `init-wizard.sh` primitives, do NOT run the full greenfield wizard:
- `prompt_secret()` (hidden `read -r -s`, L88-97) for AccessKey ID + Secret.
- Write `${DMF_BOOTSTRAP_BUNDLE_DIR}/aliyun-media/aliyun.tfvars` (`alicloud_access_key`,
  `alicloud_secret_key`, `cloudflare_api_token`), `chmod 0600` — the exact format
  `tf-apply.sh` resolves (L161). `aliyun-media` matches `aliyun-*` → DMF_PROVIDER=aliyun.
- **§0 secrets discipline:** the operator runs the entry step in their own
  terminal; the agent never captures the AK/SK. Agent runs only the non-secret
  `tf-apply.sh aliyun-media apply` and the playbooks (creds stay in the 0600
  file, never in argv/stdout).

## 8. Validation gates
1. Both Aliyun nodes `Ready` in g2r6-foa9, labelled+tainted.
2. Cross-cloud pod-to-pod MTU sanity (large-payload curl/iperf Aliyun↔Hetzner pod).
3. `mxl-hello` runs on a media node (shared-mem baseline green).
4. `mxl-fabrics-demo` target↔initiator completes a grain transfer over eth1 (tcp).

## 9. Teardown
`bin/tf-apply.sh aliyun-media destroy` (NOT `tf-destroy.sh` — it refuses Aliyun).
If Approach A: also `kubectl delete node` the two agents + remove from inventory.

## 10. Review outcomes (qwen architectural review, 2026-05-30)
1. Join approach → **DECIDED: Approach B** (separate `aliyun-media` env + ~15-line
   thin join play reusing `bootstrap-secrets.sh` token helpers). §3.1.
2. Host OS → **DECIDED: Ubuntu 24.04**; PEP 668 pip handling carries over, kernel
   modules fine — no baseline blocker. §4.
3. k3s join delta → group_vars override (`k3s_server_url: https://<ctl-tailnet-ip>:6443`,
   `k3s_flannel_iface: tailscale0`) in `aliyun-media`; no shared-playbook edits.
4. Media designation → post-join `kubectl` label/taint (keeps `300-k3s.yml` untouched).
5. Pre-flight blockers → MTU pin, nftables CIDR override, SSH allow-list, Aliyun
   SG fabrics port. §5a.
6. Mixing clouds into live g2r6-foa9 → **rejected**; reinforces Approach B.
