# Proposal: Open-Source Multicast Path Visibility (Cisco + Arista)

This proposal outlines an open-source approach to gain detailed multicast
visibility (including RP placement and tree paths) across Cisco and Arista
switches without proprietary tooling.

## Goals

- Discover and visualize multicast paths (source -> RP -> receivers).
- Support Cisco and Arista platforms.
- Remain open-source and community-driven.
- Integrate with existing monitoring (LibreNMS/Prometheus/Grafana).

## Constraints

- LibreNMS can poll PIM/IGMP/mroute data but does not build a topology tree.
- SNMP MIB support varies by platform and feature set.
- Multicast path visualization requires topology + RPF computation.

## Recommended architecture

### 1) Configuration + topology modeling (Batfish)

**Why:** Batfish can compute control-plane multicast RPF paths and RP behavior
from router configs and network topology.

**Inputs:**
- Device configs (Cisco/Arista)
- IP addressing and interfaces
- RP configuration (static or BSR)

**Outputs:**
- RPF neighbor paths
- RP resolution details
- Candidate tree paths (control-plane)

### 2) Telemetry data collection

**Sources:**
- SNMP (PIM neighbors, IGMP groups, mroute table)
- Streaming telemetry (optional: gNMI for Arista/cisco-ios-xr)

**Collectors:**
- Prometheus SNMP Exporter
- Telegraf (optional)

### 3) Data store + visualization

**Store:**
- Prometheus for time series
- Optional PostgreSQL for graph relationships

**Visualization:**
- Grafana dashboards (metric view)
- Graphviz or a lightweight web UI for tree rendering

## Data mapping model

Combine control-plane predictions (Batfish) with runtime state (telemetry):

| Data | Source | Purpose |
| --- | --- | --- |
| RP mapping | Batfish + configs | Which RP for each group |
| RPF paths | Batfish | Predicted tree path |
| IGMP joins | SNMP | Actual receivers |
| Mroute entries | SNMP | Current forwarding state |
| PIM neighbors | SNMP | Adjacency verification |

## Proposed dashboards

1) **RP map dashboard**
   - Group -> RP mapping
   - Active groups per RP

2) **Multicast tree view (graph)**
   - Nodes = switches/routers
   - Edges = RPF path
   - Color edges where mroute table confirms forwarding

3) **Health signals**
   - Missing PIM neighbors
   - IGMP joins without active mroute entry
   - RPF mismatch alerts

## Implementation phases

### Phase 1: Baseline telemetry

- Enable SNMP polling for:
  - PIM neighbor tables
  - IGMP group tables
  - Mroute entries
- Store in Prometheus
- Create Grafana panels for counts/trends

### Phase 2: Control-plane modeling

- Import configs into Batfish
- Compute RPF paths for test multicast groups
- Export path results to JSON or database

### Phase 3: Visualization

- Build a graph view (Graphviz or a simple web page)
- Overlay telemetry state (active edges/receivers)

### Phase 4: Automation

- Nightly config export -> Batfish update
- Alert on mismatches between predicted vs. actual paths

## Tooling options

### SNMP collection
- Prometheus SNMP Exporter
- LibreNMS for discovery + device inventory

### Path modeling
- Batfish (open-source, actively maintained)

### Visualization
- Grafana (metrics)
- Graphviz or D3.js (paths)

## Risks and caveats

- Multicast SNMP MIB support differs between platforms.
- Batfish models control plane, not exact data-plane forwarding.
- Network changes require config refresh in Batfish.

## Next steps

1) Decide telemetry method (SNMP only vs. SNMP + streaming).
2) Identify a minimal test topology to validate end-to-end path calculation.
3) Add Batfish container/playbook to the lab.
4) Build a prototype tree visualization for a single multicast group.
