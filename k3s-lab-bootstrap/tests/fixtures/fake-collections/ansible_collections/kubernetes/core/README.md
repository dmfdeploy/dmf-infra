# Fake kubernetes.core for the OpenBao tests

A stand-in for the two modules that the OpenBao migration tests reach:
`k8s_info` (reads fake objects from `$FAKE_STATE/k8s/<kind>-<name>.json`) and `k8s`
(logs the call to `$FAKE_STATE/k8s-mutations.log` and `$FAKE_STATE/events.log`).
Put this directory first in `ANSIBLE_COLLECTIONS_PATH` so it replaces the real
collection. Any mutation that a test did not expect shows in the log.

`k8s` keeps two objects so that a full run of the grow-HA play can read them back:
creating a StatefulSet writes `Pod-all.json` with one running pod per replica, and
deleting a Pod writes `Pod-<name>.json` with a new UID and marks the pod sealed for
the fake bao.
