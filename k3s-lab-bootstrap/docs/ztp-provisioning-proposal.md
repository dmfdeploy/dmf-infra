# Proposal: Zero-Touch Provisioning (ZTP) for Switches + Server Bootstrap

This proposal outlines a safe, open-source ZTP workflow to provision network
switches and optionally extend the same concept to server provisioning for
cluster deployment.

## Goals

- Automatically provision switches based on serial number.
- Use NetBox as the source of truth (SoT).
- Enforce safe onboarding with approval gates.
- Extend the workflow to server provisioning where practical.

## Constraints and assumptions

- DHCP broadcast is L2; running DHCP inside the k3s cluster is often not viable.
- Provisioning should occur on an isolated VLAN.
- NetBox is reachable from the provisioning network.

## Recommended architecture

### 1) Dedicated provisioning network

- Separate VLAN with strict ACLs.
- DHCP + HTTP/TFTP services are available on this VLAN.
- NetBox API is reachable (read-only token for bootstrap).

### 2) DHCP + bootstrap services (outside cluster)

Options:
- Router/firewall DHCP with vendor-specific options.
- Small VM/mini-server running **dnsmasq** or **Kea**.

Responsibilities:
- DHCP options to point devices to bootstrap scripts.
- Serve bootstrap scripts and initial configs via HTTP/TFTP.

### 3) NetBox as SoT and approval gate

NetBox stores:
- Serial number, model, site, role, IP plan, intended OS.
- Status (staged -> active) as an approval gate.

Bootstrap logic:
- Device reports serial to bootstrap script.
- Script queries NetBox by serial.
- If not found or not `staged`, apply **quarantine** config and stop.

### 4) Configuration generation + apply

- Templates stored in Git (Jinja2).
- Rendered by Ansible (or Nornir/NAPALM).
- Device pulls config via HTTP or is pushed via SSH/API.

### 5) Validation and promotion

Post-provision validation:
- LLDP neighbors match expected topology.
- Management reachability is verified.
- Device status in NetBox set to `active`.

## Safe onsite workflow (switches)

1) **Staging**: Add device to NetBox with serial and role.
2) **Cable to provisioning VLAN**.
3) **ZTP**: device fetches bootstrap, validates serial.
4) **Apply**: full config generated and installed.
5) **Validate**: health checks + neighbor checks.
6) **Promote**: move from staging to production VLAN.

## Extending to servers (cluster deployment)

If you want consistent server onboarding, use a similar pattern:

- **PXE/iPXE** for network boot.
- **DHCP + HTTP** to serve bootstrapping scripts and images.
- **NetBox device role** drives host class (control-plane vs worker).
- Cloud-init or Ignition pulls per-host config based on serial.

Suggested flow:

1) Add server asset to NetBox with serial + role.
2) PXE boot into installer.
3) Installer queries NetBox and writes cloud-init.
4) After install, node registers into inventory (e.g., Ansible or k3s).
5) Node joins cluster when approved.

## Tooling options (open-source)

**Switch ZTP**
- DHCP: Kea or dnsmasq
- Bootstrap: HTTP/TFTP + shell/Python
- Config render: Ansible + Jinja2, or Nornir/NAPALM
- SoT: NetBox

**Server bootstrap**
- DHCP + iPXE
- cloud-init + autoinstall (Debian/Ubuntu)
- NetBox for inventory and role mapping

## Pi + Tailscale provisioning node (recommended lightweight option)

This pattern works well when a full VM is not available onsite:

- **Ethernet**: connected to the provisioning VLAN (DHCP, HTTP, TFTP).
- **Wi-Fi**: uplink to the internet and **Tailscale** for access to NetBox/API.

Why it works:
- DHCP is L2-only and must live on the provisioning VLAN.
- NetBox/API access is northbound and can traverse Tailscale.
- The Pi acts as the single bootstrap point with minimal infrastructure.

Suggested stack on the Pi:
- `dnsmasq` or `kea` (DHCP + TFTP)
- `nginx` or `caddy` (HTTP bootstrap)
- `tailscale` (secure access to NetBox/API)

## Vendor ZTP matrix (EOS, IOS-XE, NX-OS)

Use DHCP class matching to point each OS to its bootstrap script.

| Platform | Bootstrap Trigger | Common DHCP Options | Notes |
| --- | --- | --- | --- |
| Arista EOS | ZTP via `ztp` script | Option 67 (bootfile) | EOS fetches `ztp.py` or shell script via HTTP/TFTP |
| Cisco IOS-XE | PnP or EEM + DHCP | Option 43 or 67 | Supports PnP (HTTP) or bootstrap config |
| Cisco NX-OS | POAP | Option 67 | POAP uses DHCP to fetch Python/CFG |

NetBox lookup flow is the same for all platforms:
1) Device reports serial to bootstrap.
2) Bootstrap queries NetBox.
3) If serial is approved -> fetch config template and apply.

## Risks and mitigations

- **DHCP misassignment**: isolate provisioning VLAN.
- **Wrong device config**: serial checks + staged status gate.
- **Drift**: run periodic compliance jobs and update NetBox.

## Drift monitoring (switches)

Recommended lightweight approach:

1) **Scheduled Ansible compliance job**
   - Fetch running config from EOS/IOS-XE/NX-OS.
   - Render intended config from NetBox + templates.
   - Compare diffs and store results.

2) **Alerting**
   - Emit a metric or webhook when drift is detected.
   - Use Prometheus/Alertmanager to notify Slack/email.

3) **Remediation policy**
   - Default to notify-only in production.
   - Allow manual approval for remediation if needed.

## Next steps

1) Confirm switch OS targets (EOS, IOS-XE, NX-OS).
2) Decide DHCP host (router vs. dedicated VM).
3) Build a minimal ZTP PoC for one switch model.
4) Add playbooks to generate configs from NetBox.
