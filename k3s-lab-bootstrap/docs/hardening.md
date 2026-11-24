# Node Hardening

> **⚠️ Numbering/commands may be historical.** Parts of this document reference an
> earlier playbook-numbering scheme (e.g. `31-forgejo`, `40-netbox-sot`, `05-harden`)
> and the pre-OpenBao `--vault-password-file` workflow. The current tree uses the
> `200/300/600` + `vertical-*` layout and the `dmf-env/bin/run-playbook.sh` OpenBao
> wrapper. Cross-check against the live `k3s-lab-bootstrap/playbooks/` tree before running.

All k3s nodes are hardened by `playbooks/05-harden.yml` (role: `roles/harden`).
Run this immediately after provisioning, before any other playbook.

## What it does

| Layer | Mechanism | Detail |
|-------|-----------|--------|
| 1 — Cloud firewall | Hetzner Cloud Firewall (`k3s-nodes`) | Applied via label selector at the network edge — traffic dropped before reaching the OS |
| 2 — OS firewall | nftables, input `policy drop` | Matches by subnet, not interface name (portable across cloud providers) |
| 3 — SSH | `prohibit-password` root, `AllowUsers`, `MaxAuthTries 3` | Key-only auth; `k3s-admin` is the non-root sudo user for all subsequent Ansible runs |
| 4 — Brute-force | fail2ban (systemd backend) | 3 attempts → 1h ban |
| 5 — Patches | unattended-upgrades | Security origins only; k3s and containerd excluded from auto-upgrade |

## What is open on the public interface

| Port | Allowed from | Purpose |
|------|-------------|---------|
| ICMP | Anywhere | Diagnostics, path MTU |
| 22/tcp | SSH allowlist only | Management (set `harden_ssh_allow_ipv4/ipv6` in inventory) |
| 80/tcp | Anywhere | VIP web traffic (MetalLB) |
| 443/tcp | Anywhere | VIP HTTPS traffic (MetalLB) |
| All other | Dropped at cloud FW | — |

The private cluster network (`harden_private_cidr`, default `10.0.0.0/28`) is fully
open at the OS level for k3s node-to-node and pod traffic.

## Non-root admin user

The role creates `harden_admin_user` (default: `k3s-admin`) with:
- NOPASSWD sudo
- SSH public key from `harden_admin_pubkey`
- Root SSH remains key-only (`prohibit-password`) as a break-glass fallback

After `05-harden.yml` completes, update `ansible_user` in your inventory to
`k3s-admin`. All subsequent playbooks run as `k3s-admin`, not root.

## Unattended upgrades and reboots

Unattended upgrades install security updates. On a fresh install, the harden
role turns off their automatic reboot (`Automatic-Reboot "false"`). Reboots from
other sources, such as power loss, provider actions, or other node
configuration, are outside this control. A reboot seals the OpenBao pod on that node, and
rebooting a second node while the first pod is still sealed takes a Raft cluster
below quorum. The role has no setting that turns scheduled reboots back on.

When `/var/run/reboot-required` exists on a node, reboot the nodes one at a time.
On a single-node site, the reboot is an OpenBao outage until you unseal the pod.

1. Run `playbooks/vertical-security/102-openbao-ha-guard.yml`. It checks that
   every OpenBao pod is unsealed and a healthy Raft voter, and refuses when the
   cluster cannot take the next restart.
2. Identify the OpenBao pod on the node, for example with
   `kubectl get pods -n openbao -o wide`. Then drain the node,
   reboot it, wait for it to rejoin, and uncordon it.
3. Check that OpenBao pod wherever it reschedules, which might not be the node
   you rebooted. A restarted pod comes back sealed. Unseal
   it with the threshold of shares, as described in "High availability and
   restarts" in `docs/openbao-bootstrap-security-model.md`.
4. Run the guard again. Reboot the next node only after it passes, which confirms
   the pod is unsealed and a healthy Raft voter.

With more than one OpenBao replica, a sealed pod counts against the
PodDisruptionBudget, so a drain waits until you unseal it. The role creates the
budget only when `openbao_replicas` is greater than 1.

On a single-node site, a drain can still block on a PodDisruptionBudget or on a
pod with local data. Find the blocking pod and resolve it. Don't override the
budget to get past it.

## Key variables

| Variable | Default | Description |
|----------|---------|-------------|
| `harden_ssh_allow_ipv4` | `[]` | IPv4 CIDRs allowed to reach port 22 |
| `harden_ssh_allow_ipv6` | `[]` | IPv6 CIDRs allowed to reach port 22 |
| `harden_admin_user` | `k3s-admin` | Non-root sudo user created on every node |
| `harden_admin_pubkey` | `""` | SSH public key for the admin user |
| `harden_private_cidr` | `10.0.0.0/28` | Private cluster network — fully allowed in nftables |
| `harden_public_tcp_ports` | `[80, 443]` | TCP ports open to the world (cloud FW + nftables) |
| `harden_fail2ban_maxretry` | `3` | Failed SSH attempts before ban |
| `harden_fail2ban_bantime` | `1h` | Ban duration |
| `harden_fail2ban_ignoreip` | `127.0.0.1/8 ::1` | Space-separated sources fail2ban never bans; the role appends the Ansible controller's address; add any extra operator CIDRs here |
| `harden_fail2ban_controller_address` | `""` | Address or CIDR the controller connects from (space-separated for several; anything else, or a /0 range, fails the play); empty reads it from the node's SSH session, and the play fails if it cannot |

## Re-running

The role is fully idempotent. Re-run at any time to enforce state or update rules:

```bash
bin/run-playbook.sh ../dmf-infra/k3s-lab-bootstrap/playbooks/05-harden.yml
```

To update only the Hetzner cloud firewall rules (e.g. after adding an SSH source IP):

```bash
bin/run-playbook.sh ../dmf-infra/k3s-lab-bootstrap/playbooks/05-harden.yml \
  --tags hetzner_firewall
```

Note: the Hetzner firewall task does not yet use tags — add `tags: hetzner_firewall`
to the `include_tasks` call in `roles/harden/tasks/main.yml` if needed.
