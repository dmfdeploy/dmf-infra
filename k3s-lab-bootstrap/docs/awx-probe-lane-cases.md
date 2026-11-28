# AWX identity and probe lane: case list

This page lists the cases that the tests for the AWX probe lane cover. Each
case has an id. The tests carry the id in a test name, a docstring or a promtool
case name, so you can search for it.

The tests are in `k3s-lab-bootstrap/tests`:

- `test_awx_identity_1027.py` runs the real roles of the real playbooks through
  `ansible-playbook`. It covers the identity cases (A, E, M) and the NetBox
  stamp (S).
- `test_awx_probe_lane_dashboard_1027.py` renders the dashboard and the alert
  rules through the real roles and runs them in `promtool`. It covers the lane
  states (C), the second custom resource (L) and the two overbroad rule
  versions (C16b).
- `roles/base/prometheus/tests/test_netbox_probe_lane_1027.promtool.yml` and
  `..._1027_tower.promtool.yml` are the promtool rule tests.
  The first runs with the default identity, the second with namespace `lab`
  and instance name `tower`.

## The method

The AWX instance has two inputs, `awx_namespace` and `awx_instance_name`. The
role `common/awx-identity` defines them once, with the defaults `awx` and `awx`.
The NetBox seed, the alert rules, the dashboard and the AWX deployer read
those two names and nothing else.

Three filters in that role turn the inputs into every other value, where the
value is used:

- `awx_instance_name | awx_service_name` returns `<name>-service`.
- `awx_instance_name | awx_service_port` returns 80, the port of the operator's
  Service.
- `awx_instance_name | awx_probe_instance(awx_namespace)` returns
  `<name>-service.<namespace>.svc.cluster.local:80`.

A filter reads its arguments only, so extra vars cannot change one reader and
leave the others.

## Fixtures

The probe series carry the labels that a live scrape gives them: `job`,
`exposure` and `instance`, with no `app` label. The AWX custom resource series
carries the labels that kube-state-metrics gives it, with `name` and `namespace`
set for each case. Its `instance` label is a documentation address in place of
the pod address. When a case needs two series for one resource, the second
differs only in `instance`.

## Lane states

"Asleep" means the autoscale helper set `web_replicas: 0` on the custom
resource, so the metric reads 0 and the probe fails on purpose.

| Id | Case | Lane reads | Dashboard shows | Alert |
|---|---|---|---|---|
| C1 | Awake: resource 2, probe 1 | 1 | up (1) | silent |
| C1b | Awake, probe failing: resource 2, probe 0 | 0 | failed (0) | fires after 10 minutes |
| C2 | Asleep through autoscale: resource 0, probe 0 | 0 | asleep (2) | silent |
| C2b | Asleep, probe still succeeding: resource 0, probe 1 | 1 | asleep (2) | silent |
| C3a | Scaled to zero by hand through the resource | 0 | asleep (2) | silent, the same series as C2 |
| C3b | Deployments at 0, resource still at 2 | 0 | failed (0) | fires after 10 minutes |
| C4 | AWX not deployed: metric absent, probe 0 | 0 | failed (0) | fires after 10 minutes |

## The two inputs and the four readers

The four readers are the NetBox seed, the alert rules, the dashboard and the
deployer. Each case compares all four against one expected set of values: name,
namespace, service, port and probe instance.

| Id | Case | Expected |
|---|---|---|
| A1 | Default inputs | `awx`, `awx`, `awx-service`, 80, `awx-service.awx.svc.cluster.local:80` |
| A2 | `awx_namespace: lab` | `awx`, `lab`, `awx-service`, 80, `awx-service.lab.svc.cluster.local:80` |
| A3 | `awx_instance_name: tower` | `tower`, `awx`, `tower-service`, 80, `tower-service.awx.svc.cluster.local:80` |
| A4 | Both: `lab` and `tower` | `tower`, `lab`, `tower-service`, 80, `tower-service.lab.svc.cluster.local:80` |
| A5 | The identities of A1 to A4 set with `-e` instead of the inventory | the same values as A1 to A4 |
| A6 | An unsafe value (quote, backslash, brace, dollar sign, newline) in either input, through the inventory and through `-e` | all four readers fail with the shape message, the stub receives no request and no output is written |

The deployer reads the service name and port in two IngressRoute definitions in
its tasks. A static check reads both definitions and verifies that each uses the
filters, that no `-service` literal remains, and that the rendered port is 80.

### Removed names

Each case sets one name, once through the inventory and once through `-e`, on top
of A1, and compares all four readers with A1. "Inert" means that the values do
not change and the play succeeds.

| Id | Name set | Value used | Result |
|---|---|---|---|
| E1 | `awx_probe_instance` | `other.awx.svc.cluster.local:80` | inert |
| E2 | `awx_probe_service` | `other` | inert |
| E3 | `awx_probe_port` | `8080` | inert |
| E4 | `prometheus_alert_awx_namespace` | `lab` | inert |
| E5 | `prometheus_alert_awx_instance_name` | `tower` | inert |
| E6 | `prometheus_alert_awx_probe_instance` | `other.lab.svc.cluster.local:80` | inert |
| E7 | `grafana_awx_namespace` | `lab` | inert |
| E8 | `grafana_awx_instance_name` | `tower` | inert |
| E9 | `grafana_awx_probe_instance` | `other.lab.svc.cluster.local:80` | inert |
| E10 | `dmf_born_inventory_app_cluster_coords` with an `awx` key | any values | the seed play fails at its first task, naming the key and the two inputs, and the stub receives no request |
| E11 | `dmf_born_inventory_app_cluster_coords` replaced by a dictionary without `awx` | other apps only | succeeds, and the stub receives the AWX stamp built from the inputs |
| E12 | E1 to E9 together with the identity of A4 | as above | the values of A4, unchanged |

### Missing, empty and null inputs

"Empty" is `""` and "null" is YAML null. The empty and null cases set the value
through the inventory and through `-e`. "Undefined" means that the role runs from a
copy of the roles tree whose `meta/main.yml` lists no dependency, so the identity
defaults never load.

| Id | Reader | Input | Case | Result |
|---|---|---|---|---|
| M1 | seed | `awx_namespace` | empty | fails before any request, naming both inputs |
| M2 | seed | `awx_namespace` | null | same |
| M3 | seed | `awx_namespace` | undefined | same, from the seed's own first task |
| M4 | seed | `awx_instance_name` | empty, null, undefined | same as M1 to M3 |
| M5 | alert rules | each input | empty, null, undefined | fails at the role's first task, naming both inputs, and writes no rules |
| M6 | dashboard | each input | empty, null, undefined | fails at the first task and renders no ConfigMap |
| M7 | deployer | each input | empty, null, undefined | fails at the first task and renders nothing |
| M8 | each reader | both inputs | dependency dropped, both inputs supplied, the template still calls a filter | fails with a missing-filter error at the first use, after the reader's own check passed |
| M9 | structure | none | every reader lists `common/awx-identity` in `meta/main.yml` and starts with the presence check; the identity defaults hold the two inputs and no derived name; the deployer's defaults hold neither input | the test fails if any of these stops holding |

M8 does not claim that a missing dependency is always detected at run time. When
an inventory or another role already provides the inputs and the filters, a
dropped dependency is not visible in the play. M9 is the check that holds.

## The NetBox stamp

The tests run the real `app-service.yml` against a local server that stands in
for the NetBox API and records every request. The stamp sets the custom fields
`cluster_service` (text), `cluster_namespace` (text) and `cluster_port`
(integer). The tests assert the type that the custom field definition declares.

| Id | Case | Stub setup | Stamp |
|---|---|---|---|
| S1 | AWX, default inputs | lookup returns id 7 | `awx-service`, `awx`, 80 |
| S2 | AWX, `lab` and `tower` | id 7 | `tower-service`, `lab`, 80 |
| S3 | AWX, namespace only (`lab`) | id 7 | `awx-service`, `lab`, 80 |
| S4 | AWX, name only (`tower`) | id 7 | `tower-service`, `awx`, 80 |
| S5 | AWX, coordinates dictionary replaced without an `awx` key | id 7 | the body of S1 |
| S6 | AWX, lookup empty, create returns id 9 | id from create | PATCH to service 9 with the body of S1 |
| S7 | AWX, lookup empty, create returns no id | no id | no stamp request |
| S8 | AWX, `-e awx_probe_instance=other...` and `-e awx_probe_service=other` | id 7 | the body of S1 |
| S9 | Another app, `forgejo` | id 7 | its own dictionary entry, unchanged |
| S10 | Another app whose entry has an empty service | id 7 | no stamp request |

## Lookalike lanes

These are regression guards: the earlier rule also passes them. The cases that
tell the earlier rule from the current one are the lane states above and the two
overbroad versions of the rule in C16b, which must fail.

| Id | Case | Lane reads | Dashboard shows | Alert |
|---|---|---|---|---|
| C16 | AWX asleep and another app's lane at 0, with the address `awx-service-other.awx.svc.cluster.local:80`, `awx-service.awx.svc.cluster.local:8080` or `awx-service.awx.svc.cluster.local:80/path` | 0 | failed (0), never dropped | fires |
| C16b | Two overbroad versions of the rule: one drops every lane while the resource reads 0, one drops every lane with an `awx-` prefix | n/a | n/a | both must fail the lane cases |
| C10 | Inputs `lab` and `tower`, `tower` asleep, lane `tower-service.lab.svc.cluster.local:80` at 0 | 0 | asleep (2) | silent |
| C10b | Inputs `lab` and `tower`, `tower` asleep, lane at the default address `awx-service.awx.svc.cluster.local:80` at 0 | 0 | failed (0) | fires |
| C17 | Inputs `lab` and `tower`, `tower` asleep, lane `tower-service.lab.svc.cluster.local:8080` at 0 | 0 | failed (0) | fires |

## A second custom resource

The configured resource is `awx` in `awx` (default inputs) or `tower` in `lab`
(the tower inputs), written as (namespace, name). Other resource 1 has the same
namespace and another name: (`awx`, `awx2`) and (`lab`, `tower2`). Other
resource 2 has the same name in another namespace: (`awx2`, `awx`) and
(`lab2`, `tower`). The AWX lane reads 0 in every case.

| Id | Configured resource | Other resource | Inputs | Alert | Dashboard |
|---|---|---|---|---|---|
| L1 | asleep (0) | 1, awake (2) | default | silent | asleep (2) |
| L2 | awake (2) | 1, asleep (0) | default | fires after 10 minutes | failed (0) |
| L3 | asleep (0) | 2, awake (2) | default | silent | asleep (2) |
| L4 | awake (2) | 2, asleep (0) | default | fires after 10 minutes | failed (0) |
| L5 | asleep (0) | 1, awake (2) | tower | silent | asleep (2) |
| L6 | awake (2) | 1, asleep (0) | tower | fires after 10 minutes | failed (0) |
| L7 | asleep (0) | 2, awake (2) | tower | silent | asleep (2) |
| L8 | awake (2) | 2, asleep (0) | tower | fires after 10 minutes | failed (0) |
| C19a | Two series for the configured resource, both 0 | none | default | silent | asleep (2), one lane series |
| C19b | The same two series, 0 and 2 | none | default | fires, because the aggregate takes the larger value | failed (0) |

## Filter discovery

The filters must resolve in every place that renders with them:

- each reader's real role list, including the identity role as a dependency and
  the post-task rendering, covered by the A rows;
- both promtool renders in the CI step (the default inputs and the tower
  inputs), which `test_ci_promtool_renders_resolve_the_filters` runs from the
  workflow file itself.

## Assumption

The filters return `<name>-service` and port 80, as the operator chart in use
creates the Service. The IngressRoute task already used port 80, which is
evidence and not a statement from the operator. No cluster was read for these
tests. A chart upgrade needs a check against a live Service.
