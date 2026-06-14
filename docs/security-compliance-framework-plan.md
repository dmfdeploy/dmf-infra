# Security And Compliance Framework Plan

This document compares candidate security and compliance frameworks against the
EBU Dynamic Media Facility (DMF) Reference Architecture and proposes a pragmatic
adoption path for this lab/project.

It is not legal advice. Regulatory applicability, especially GDPR and NIS2, should be
confirmed with counsel or the relevant compliance owner before making contractual
claims.

## Sources

- EBU DMF Reference Architecture V2.0, local copy:
  `<operator-downloads>/EBU_White_Paper_The_Dynamic_Media_Facility_Reference_Architecture.pdf`
- EBU DMF Reference Architecture publication page:
  <https://tech.ebu.ch/publications/white-paper-2026-04-15>
- Vanta additional frameworks list:
  <https://www.vanta.com/products/additional-frameworks>
- NIST Cybersecurity Framework 2.0:
  <https://www.nist.gov/node/1840561>
- CIS Controls v8.1:
  <https://www.cisecurity.org/controls/v8-1>
- ISO/IEC 27001 overview:
  <https://www.iso.org/standard/27001>
- CSA Cloud Controls Matrix:
  <https://cloudsecurityalliance.org/research/cloud-controls-matrix>
- GDPR / EU data protection rules:
  <https://commission.europa.eu/law/law-topic/data-protection/eu-data-protection-rules_en>
- NIS2 Directive overview:
  <https://digital-strategy.ec.europa.eu/en/policies/nis2-directive>
- ISO 22301 overview:
  <https://www.iso.org/standard/75106.html>
- EBU R 143 cybersecurity recommendation:
  <https://tech.ebu.ch/contents/publications/r/cybersecurity-for-media-vendor-systems-software--services.html>
- EBU R 160 vulnerability management:
  <https://tech.ebu.ch/publications/r160>

## Executive Recommendation

Do not start with a formal audit framework as the primary design driver. Start with a
control and evidence model that matches the DMF architecture, then map it to audit
frameworks.

Recommended order:

1. **EBU DMF + EBU R 143/R 160 as the media-domain baseline.**
   Use DMF for architecture/lifecycle scope, R 143 for media vendor/product security
   expectations, and R 160 for vulnerability handling.
2. **NIST CSF 2.0 as the governance map.**
   Use CSF's Govern, Identify, Protect, Detect, Respond, Recover functions to structure
   risks, control ownership, and executive reporting.
3. **CIS Controls v8.1 as the first technical implementation checklist.**
   Use IG1 immediately, then IG2 for this Kubernetes/media-platform stack.
4. **ISO/IEC 27001 as the target management-system framework.**
   Treat ISO 27001 as the long-term ISMS spine for policy, risk management, supplier
   management, access control, evidence, and continual improvement.
5. **CSA CCM and ISO/IEC 27017 for cloud/hybrid security.**
   Use these when DMF workloads run in public cloud, remote clusters, or a shared
   service model.
6. **GDPR as a mandatory privacy overlay where personal data exists.**
   Operator accounts, access logs, audit records, identity provider data, support
   records, and production metadata may all become personal data.
7. **NIS2 and ISO 22301 as resilience and critical-service overlays.**
   Use NIS2 as a scoping question for EU operators or service providers, and ISO 22301
   to formalize continuity for live-production service availability.

SOC 2, PCI DSS, HIPAA, FedRAMP, CMMC, DORA, HITRUST, and similar frameworks should
remain customer/regulatory-triggered, not baseline work, unless the project scope
changes.

## EBU DMF Security And Compliance Reading

The DMF white paper is not a compliance framework. It is a reference architecture
for software-defined media production. For compliance purposes it provides the
scope model we need to avoid generic security paperwork.

### Architecture Scope

DMF defines six horizontal layers:

- Infrastructure
- Host Platform
- Container Platform
- Media Exchange
- Media Functions
- Application & User Interface

It also defines cross-cutting verticals:

- Orchestration
- Control
- Monitoring
- Security

For this repo, the current lab maps strongly to the lower and middle layers:

- Infrastructure and host automation: Ansible, inventory, lifecycle playbooks.
- Container platform: k3s, Traefik, Longhorn, External Secrets Operator.
- Security vertical: OpenBao, Authentik, policy-as-code, network policies, TLS.
- Monitoring vertical: Prometheus, Grafana, Loki/Promtail.
- Orchestration/control: AWX, Forgejo, NetBox SoT, lifecycle playbooks.
- Media/application extension point: Zot registry, future DMF workloads and UIs.

### Lifecycle Scope

DMF's workload lifecycle is useful as an evidence lifecycle:

- **Design:** requirements, threat model, data classification, architecture decisions.
- **Plan:** capacity, resilience, authorization model, supplier/cloud assumptions.
- **Provision:** IaC run logs, asset inventory, image provenance, secrets created.
- **Configure:** policy-as-code, access grants, certificates, network segmentation.
- **Operate:** monitoring, user authentication, incident detection, privileged changes.
- **Finalise & Review:** deprovisioning, authorization removal, retained records,
  lessons learned.
- **Monitor & Update:** vulnerability management, CI/CD, patch evidence, control
  drift checks.

This means each Ansible role/playbook should eventually be able to answer:

- Which DMF layer does this affect?
- Which lifecycle stage does this implement?
- Which controls does it satisfy?
- What evidence is produced?
- Who owns the control?
- How is drift detected?

### Security Themes From DMF

The DMF white paper's security posture is directly compatible with modern compliance:

- Zero trust.
- AAA for users and devices.
- Least privilege.
- Segmentation to prevent lateral movement.
- Continuous monitoring and validation.
- Encrypted and authorized APIs.
- Multi-tenancy isolation.
- Vulnerability management.
- Usable SSO-based access at the UI layer.

These themes align well with NIST CSF, CIS Controls, ISO 27001, CSA CCM, GDPR, and
NIS2. The missing piece is a project-specific control register and evidence model.

## Framework Relevance Matrix

| Framework | Relevance | Why It Matters Here | Recommended Treatment |
|---|---:|---|---|
| EBU DMF RA V2.0 | Critical | Defines the target architecture, layers, lifecycle, multi-vendor and multi-workload model. | Use as the architecture scope and terminology baseline. |
| EBU R 143 | Critical | Media-specific cybersecurity expectations for systems, software, services, SaaS, vendors, auth, logging, encryption, cloud segregation. | Use as the media security acceptance checklist. |
| EBU R 160 | Critical | Media-specific vulnerability management and security testing guidance. | Use for vulnerability disclosure, scanning, remediation SLAs, and vendor coordination. |
| NIST CSF 2.0 | High | Provides a broad, non-prescriptive risk governance structure with Govern, Identify, Protect, Detect, Respond, Recover. | Use as the top-level control taxonomy and maturity reporting model. |
| CIS Controls v8.1 | High | Prioritized technical safeguards for common attacks; practical for a small lab evolving into a service. | Implement IG1 now, IG2 next; use IG3 only for high-risk/customer-driven controls. |
| ISO/IEC 27001 | High | International ISMS standard; strong fit for EU/international customer trust. | Long-term target. Build policies/evidence now, defer certification until operations stabilize. |
| CSA CCM | High for cloud/hybrid | Cloud control framework and shared-responsibility model; maps well to DMF public-cloud and remote-cluster scenarios. | Use for cloud provider/customer split and vendor assessments. |
| ISO/IEC 27017 | Medium-High | Cloud-specific controls for cloud providers and customers. | Use when designing cloud-hosted or multi-site DMF clusters. |
| ISO/IEC 27018 | Medium | PII protection in public clouds acting as PII processors. | Use if the platform processes customer/operator PII in public cloud. |
| GDPR | High if EU personal data exists | Applies to personal data such as identities, logs, audit trails, support data, and possibly production metadata. | Mandatory overlay: data inventory, minimization, retention, access rights, processor/controller roles. |
| NIS2 | Medium-High, context-dependent | Relevant if operating as or for EU critical/digital infrastructure, cloud/data-centre/managed-service providers, or regulated broadcasters. | Run a formal scoping assessment before making claims. Prepare controls that NIS2 expects: risk management, incident reporting, supply-chain security. |
| ISO 22301 | Medium-High | DMF use cases emphasize resilience, workload relocation, and service continuity. | Use as continuity/resilience planning model, especially for live production. |
| SOC 2 | Medium | Useful for US/B2B service assurance, especially managed SaaS/platform operations. | Defer until product/service boundary and customer commitments are clear. |
| ISO/IEC 27701 | Medium | Privacy extension to ISO 27001. | Consider after GDPR data inventory is real and ISO 27001 program exists. |
| MVSP | Medium | Lightweight baseline for SaaS/vendor security questionnaires. | Use as a quick external-facing baseline if customer questionnaires appear before audit readiness. |
| Cyber Essentials | Medium for UK customers | UK baseline for common cyber threats. | Customer/procurement-triggered. Could map from CIS IG1. |
| ISO 42001 / NIST AI RMF / EU AI Act | Watchlist | Relevant only if AI systems become part of production workflows, automated editorial decisions, or product claims. | Track, but do not baseline yet. |
| PCI DSS | Low | Only relevant if processing cardholder data. | Out of scope unless payments are added. |
| HIPAA / HITRUST | Low | Healthcare-specific. | Out of scope unless handling PHI. |
| FedRAMP / NIST 800-53 / NIST 800-171 / CMMC | Low | US government/federal/defense procurement. | Customer-triggered only. |
| DORA / 23 NYCRR 500 / CPS 234 / CRI Profile / OFDSS | Low | Financial services regulatory frameworks. | Out of scope unless serving financial-sector regulated entities. |
| TISAX | Low | Automotive supply chain. | Out of scope. |
| CJIS | Low | US criminal justice data. | Out of scope. |
| SOX ITGC | Low | Financial reporting control environment. | Out of scope unless this platform supports financial reporting systems. |
| ISO 9001 | Low-Medium | Quality management, not security. | Optional if product delivery quality certification becomes useful. |

## Recommended Baseline Control Model

Use a local control register that maps each control to:

- DMF layer.
- DMF lifecycle stage.
- Framework references.
- Technical implementation.
- Evidence source.
- Owner.
- Review cadence.
- Status.

Suggested control ID scheme:

- `DMF-GOV-*` for governance and risk.
- `DMF-IAM-*` for identity and access.
- `DMF-SEC-*` for technical security.
- `DMF-VULN-*` for vulnerability management.
- `DMF-LOG-*` for logging and monitoring.
- `DMF-BC-*` for continuity/resilience.
- `DMF-PRIV-*` for privacy.
- `DMF-SUP-*` for supplier and third-party controls.

Example controls:

| Control ID | Control | DMF Mapping | Framework Mapping | Evidence |
|---|---|---|---|---|
| DMF-IAM-001 | Human users authenticate through SSO/MFA where supported. | Application/UI, Operate | EBU R143, CIS 6, ISO 27001, NIST Protect | Authentik config, group mappings, login logs. |
| DMF-IAM-002 | Service accounts follow naming, least privilege, and token rotation conventions. | Security vertical, Configure/Operate | NIST Protect, CIS 5/6, ISO 27001 | OpenBao policies, ESO AppRole metadata, rotation logs. |
| DMF-SEC-001 | OpenBao policy-as-code is reconciled and validated on rerun. | Security vertical, Configure/Monitor | NIST Govern/Protect, CIS 4/6, ISO 27001 | OpenBao playbook logs, capability checks. |
| DMF-LOG-001 | Platform logs and metrics are collected centrally. | Monitoring vertical, Operate | CIS 8, NIST Detect, ISO 27001 | Loki/Promtail/Grafana/Prometheus state. |
| DMF-VULN-001 | Images and host packages are patched and vulnerability findings tracked. | Host/Container, Monitor & Update | EBU R160, CIS 7, NIST Identify/Protect | Scan reports, patch run logs, exceptions register. |
| DMF-BC-001 | Cluster rebuild and app restore are tested. | All layers, Recover/Finalise | ISO 22301, NIST Recover, SOC 2 Availability | Rebuild runbook, restore evidence, RTO/RPO notes. |
| DMF-PRIV-001 | Personal data processed by identity/logging systems is inventoried. | App/UI, Monitoring, Security | GDPR, ISO 27701, ISO 27018 | Data inventory, retention policy, access request procedure. |

## Current Repo Evidence Opportunities

The repo already has useful evidence sources:

- Ansible playbooks and roles as infrastructure-as-code evidence.
- `cluster-ready` role as readiness gate evidence.
- OpenBao policy generation and policy reconciliation checks.
- Authentik group/provider configuration for identity evidence.
- External Secrets Operator and OpenBao for secret delivery evidence.
- Prometheus/Grafana/Loki/Promtail for monitoring/logging evidence.
- NetBox SoT and born-inventory for asset/source-of-truth evidence.
- Forgejo/AWX for automation and change traceability.
- Zot registry for image custody and deployment provenance.
- Runbook playbooks for health checks and operational validation.

Missing or weak evidence areas:

- Formal asset inventory mapped to DMF layers.
- Formal data classification and retention.
- Formal risk register.
- Written incident response process.
- Written vulnerability management process tied to EBU R160.
- Supplier/cloud shared-responsibility register.
- Backup/restore and rebuild test evidence.
- Access review cadence and evidence.
- Change approval/review trail for production-impacting changes.
- Container image scanning/signing/SBOM evidence.

## Implementation Plan

### Phase 0 — Scope And Ownership

Goal: define what is in the compliance boundary.

Deliverables:

- Define whether this is a lab, managed platform, appliance, SaaS, or customer-hosted reference implementation.
- Name owners for security, operations, privacy, and compliance.
- Decide initial framework targets: recommended `EBU DMF + EBU R143/R160 + NIST CSF + CIS IG1`.
- Create a first control register file under `docs/` or a structured data file under `compliance/`.

Exit criteria:

- A written scope statement exists.
- A control owner exists for each top-level domain.
- Out-of-scope frameworks are explicitly recorded.

### Phase 1 — Baseline Controls

Goal: make the current lab defensible and inspectable.

Deliverables:

- DMF layer inventory for every major component.
- Data inventory for identity, logs, secrets metadata, app data, registry data.
- Initial risk register.
- Initial access model: users, groups, service accounts, AppRoles, break-glass.
- Initial evidence checklist mapped to NIST CSF and CIS IG1.
- Secret artifact hygiene policy: no root tokens or durable credentials in `/tmp`, playbook logs, or shell history.

Technical work:

- Add a read-only evidence collection runbook.
- Add a secret-leak scan for `/tmp`, playbook logs, and repo diffs.
- Add access review output for Authentik/OpenBao/Kubernetes.
- Add a vulnerability scan path for container images and host packages.

Exit criteria:

- Evidence can be collected without manual shell spelunking.
- Access and secret state can be reviewed after every rebuild.
- CIS IG1 gaps are listed with owners.

### Phase 2 — DMF-Aligned Hardening

Goal: map technical controls to DMF layers and lifecycle.

Deliverables:

- DMF layer-to-control matrix.
- Network segmentation design.
- Logging and retention policy.
- Vulnerability management process aligned to EBU R160.
- Supplier and container image provenance process.
- Backup/rebuild/restore process with test evidence.

Technical work:

- Enforce OpenBao policy reconciliation checks.
- Add Kubernetes network policies for core namespaces.
- Add image scanning/SBOM/signature strategy for Zot.
- Add cert/key lifecycle evidence.
- Add log retention and alerting baseline.
- Add backup and restore tests for OpenBao, Longhorn app data, NetBox, Forgejo, and AWX.

Exit criteria:

- CIS IG1 is substantially complete.
- CIS IG2 has a prioritized backlog.
- Every DMF layer has controls for identity, logging, vulnerability management, and recovery.

### Phase 3 — Compliance Readiness

Goal: prepare for externally recognizable frameworks without committing to audit too early.

Deliverables:

- NIST CSF current profile and target profile.
- ISO 27001 statement of applicability draft.
- GDPR record of processing draft if personal data exists.
- NIS2 applicability assessment.
- CSA CCM shared responsibility matrix for cloud/hybrid deployment.
- ISO 22301 continuity scenarios for live-production use cases.

Technical work:

- Add automated evidence exports for access reviews, policy checks, backup tests, vulnerability scans, and incident drills.
- Add customer/security questionnaire response pack.
- Add a trust package with architecture diagram, security model, and framework mapping.

Exit criteria:

- The project can answer a customer security questionnaire consistently.
- ISO 27001/SOC 2 readiness gaps are known.
- NIS2/GDPR applicability is documented.

### Phase 4 — External Assurance

Goal: only pursue audit/certification when scope and evidence are stable.

Likely paths:

- ISO 27001 certification if targeting EU/international media operators.
- SOC 2 Type II if selling managed services or SaaS to US/B2B customers.
- Cyber Essentials if UK procurement requires it.
- CSA STAR/CAIQ or ISO 27017 mapping if public cloud delivery becomes central.

Exit criteria:

- 3-6 months of stable operational evidence.
- Access reviews are repeatable.
- Incident/vulnerability processes have been exercised.
- Backup and restore tests are repeatable.
- Customer-facing commitments match actual controls.

## Proposed Immediate Next Steps

1. Create `docs/security-control-register.md` with the first 25 controls.
2. Create `docs/security-risk-register.md` with initial risks:
   policy drift, leaked temp secrets, loss of OpenBao break-glass, image supply chain,
   multi-tenant workload escape, stale patch state, restore failure.
3. Add a `playbooks/runbooks/security-evidence-check.yml` runbook that collects:
   OpenBao capability checks, Authentik groups, Kubernetes node/pod status, ESO health,
   certificate status, backup status, and log pipeline status.
4. Add a `scripts/scan-sensitive-artifacts.sh` utility for `/tmp`, playbook logs, and repo diffs.
5. Map existing OpenBao/Zot/Auth/ESO controls to CIS IG1 and NIST CSF.
6. Decide whether this project is aiming for:
   `internal lab only`, `customer-hosted reference platform`, or `managed service`.

The third option changes the compliance target substantially: managed service pushes
SOC 2, ISO 27001, GDPR processor obligations, CSA CCM, and possibly NIS2 much
closer to mandatory.

## Frameworks To Defer Unless Triggered

- **PCI DSS:** only if cardholder data is processed.
- **HIPAA/HITRUST:** only if PHI or healthcare customers enter scope.
- **FedRAMP/NIST 800-53/CMMC/NIST 800-171:** only for US federal/defense work.
- **DORA/23 NYCRR 500/CPS 234/CRI/OFDSS:** only for financial services.
- **CJIS:** only for US criminal justice data.
- **TISAX:** only for automotive customers.
- **SOX ITGC:** only if the platform supports financial reporting controls.
- **ISO 42001/NIST AI RMF/EU AI Act:** only when AI functionality becomes part of
  product behavior, editorial decisioning, or customer claims.

## Open Questions

- ~~Is the intended target a lab, reference implementation, customer-hosted product,
  or managed service?~~ **Proposed answer 2026-05-11 by [ADR-0020](https://github.com/dmfdeploy/dmfdeploy/blob/main/docs/decisions/0020-deployment-scope-and-regulatory-posture.md)
  — pending promotion from Proposed to Accepted. DMF ships in three
  explicitly-named modes (OSS self-host, managed `dmfdeploy.io`,
  flypack), each with its own regulatory posture and binding
  architectural constraints. This plan's framework applies to all three
  modes; the [Pre-Release Compliance Readiness Plan](https://github.com/dmfdeploy/dmfdeploy/blob/main/docs/plans/DMF%20Pre-Release%20Compliance%20Readiness%20Plan%202026-05-11.md)
  enumerates per-mode gates.**
- Will customer or production personal data be stored, or only lab identities and logs?
  Mode-dependent: Mode A (OSS) — operator's own choice; Mode B (managed) —
  yes, addressed by the GDPR controller/processor pack at Tier B exit.
  In Mode B, dmfdeploy.io is expected to be processor for customer-hub
  data and controller for its own account, billing, support,
  security-log, and vulnerability-intake data. Mode C (flypack) — yes,
  with portable-media controls per Tier C.
- Will the platform host multiple organizations or productions at the same time?
  Proposed answer: cluster-per-tenant per ADR-0020 B.3. No.
- Will workloads run in public cloud, customer premises, or both?
  Both, by mode: Mode A self-host anywhere; Mode B cloud or customer
  premises; Mode C portable.
- Can the current OpenBao auto-unseal design support managed-service
  customer hubs? No. ADR-0011 is an experiment-phase tradeoff and must be
  retired, superseded, or scoped away from customer hubs before Mode B
  exit. Managed mode requires customer-side Shamir quorum custody.
- What recovery time and recovery point objectives are required for live production?
  Open until Tier B; the Pre-Release Compliance Readiness Plan §A.8
  requires this to be answered (with an RPO/RTO claim or an explicit
  "no claim until Tier B") before the OSS push.
- Which party owns vulnerability disclosure and patch SLAs for third-party media
  functions? Operator owns intake (`security@dmfdeploy.io`), per Tier B.6.
  Upstream coordination via the supplier register (Tier A.6).
- Are customer security questionnaires expected in the next quarter?
  Not before Tier B exit.

## Pre-release gates

The framework above describes *what controls* the project intends to
implement and *which frameworks* they map to. It does not describe *when
each mode is allowed to go live*.

The companion document
[`docs/plans/DMF Pre-Release Compliance Readiness Plan 2026-05-11.md`](https://github.com/dmfdeploy/dmfdeploy/blob/main/docs/plans/DMF%20Pre-Release%20Compliance%20Readiness%20Plan%202026-05-11.md)
defines three tiered gates corresponding to the three deployment modes
proposed in ADR-0020:

- **Tier A** — before the first GitHub push (Mode A go-live)
- **Tier B** — before the first managed-service customer (Mode B go-live)
- **Tier C** — before the first flypack ships (Mode C go-live)

The operational checklist is at
[`docs/processes/pre-release-compliance-checklist.md`](https://github.com/dmfdeploy/dmfdeploy/blob/main/docs/processes/pre-release-compliance-checklist.md).

This framework plan's Phase 4 ("external assurance" — ISO 27001
certification, SOC 2 Type II) follows Tier B exit by 6+ months of
stable operations and is not gated by this checklist.

Until ADR-0020 is Accepted, the gates above are treated as draft
readiness gates. They should not be used as external claims.
