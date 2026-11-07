# Fake kubernetes.core for the OpenBao tests

A stand-in for the two modules that the OpenBao migration tests reach:
`k8s_info` (reads fake objects from `$FAKE_STATE/k8s/<kind>-<name>.json`) and `k8s`
(logs the call to `$FAKE_STATE/k8s-mutations.log` and changes nothing). Put this
directory first in `ANSIBLE_COLLECTIONS_PATH` so it replaces the real collection.
Any mutation that a test did not expect shows in the log.
