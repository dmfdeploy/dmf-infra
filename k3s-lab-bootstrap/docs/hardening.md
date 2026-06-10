# Node Hardening

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

`Automatic-Reboot "true"` is set, but **this does not reboot nightly**. A reboot
only occurs if an upgrade sets `/var/run/reboot-required` (typically a kernel or
libc update). Most nightly runs install nothing or install packages that need no
reboot.

### ⚠️ Known issue: simultaneous reboots on a multi-node cluster

All nodes share the same reboot window (`harden_reboot_time`, default `02:00 UTC`).
If a kernel update lands on all nodes on the same night, all three will reboot
simultaneously, taking the cluster down completely.

**Workarounds (choose one):**

**Option A — Stagger reboot times per node** (pragmatic for a lab)

Set per-host variables in your inventory:

```ini
# hosts.ini
k3s-node-01 ... harden_reboot_time="02:00"
k3s-node-02 ... harden_reboot_time="02:20"
k3s-node-03 ... harden_reboot_time="02:40"
```

Nodes don't coordinate, but a reboot takes ~2-3 minutes so 20-minute gaps are
sufficient for a 3-node cluster to stay healthy throughout.

**Option B — Disable auto-reboot, use a drain/reboot playbook** (production approach)

Set `harden_reboot_time` to a sentinel and `Automatic-Reboot "false"` (override
the template), then run a playbook that:
1. `kubectl cordon && kubectl drain` the node
2. Reboots and waits for it to rejoin
3. `kubectl uncordon`
4. Repeats for the next node

This is the correct approach if uptime matters. Not yet implemented — tracked as
future work.

## Key variables

| Variable | Default | Description |
|----------|---------|-------------|
| `harden_ssh_allow_ipv4` | `[]` | IPv4 CIDRs allowed to reach port 22 |
| `harden_ssh_allow_ipv6` | `[]` | IPv6 CIDRs allowed to reach port 22 |
| `harden_admin_user` | `k3s-admin` | Non-root sudo user created on every node |
| `harden_admin_pubkey` | `""` | SSH public key for the admin user |
| `harden_private_cidr` | `10.0.0.0/28` | Private cluster network — fully allowed in nftables |
| `harden_public_tcp_ports` | `[80, 443]` | TCP ports open to the world (cloud FW + nftables) |
| `harden_reboot_time` | `02:00` | UTC time for unattended-upgrades auto-reboot |
| `harden_fail2ban_maxretry` | `3` | Failed SSH attempts before ban |
| `harden_fail2ban_bantime` | `1h` | Ban duration |

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
