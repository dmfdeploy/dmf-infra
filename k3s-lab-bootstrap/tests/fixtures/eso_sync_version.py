"""The synced resource version that ESO v2.3.0 writes, for the fake kubectl and the
tests. ESO builds "<generation>-<hash>" (pkg/controllers/util/util.go:
GetResourceVersion and HashMeta); the hash is SHA3-224 of Go's "%+v" rendering of
the annotations and labels (runtime/esutils/utils.go: ObjectHash). Go prints a map as
map[key:value key:value] with the keys in byte order, and map[] for an empty or absent
map.

Run as a script, it writes a synced status for the object in the file at PATH:
  python3 eso_sync_version.py PATH [scheme]
The scheme "other" writes a version that is built differently from ESO's, as another
ESO release might."""
import hashlib
import json
import sys


def _go_map(mapping):
    return "map[%s]" % " ".join("%s:%s" % pair for pair in sorted((mapping or {}).items()))


def synced_resource_version(generation, annotations, labels, scheme="eso"):
    if scheme == "other":
        payload = json.dumps([annotations or {}, labels or {}], sort_keys=True)
        return "%s-%s" % (generation, hashlib.sha256(payload.encode()).hexdigest()[:24])
    rendered = "{annotations:%s labels:%s}" % (_go_map(annotations), _go_map(labels))
    return "%s-%s" % (generation, hashlib.sha3_224(rendered.encode()).hexdigest())


def synced_status(data, scheme="eso", extra_annotations=None, refresh_time="2999-01-01T00:00:00Z"):
    """The status of an object that ESO has synced. extra_annotations is metadata
    that the version covers although the object does not carry it (an earlier change
    that ESO published)."""
    meta = data["metadata"]
    annotations = dict(meta.get("annotations") or {})
    annotations.update(extra_annotations or {})
    return {
        "refreshTime": refresh_time,
        "syncedResourceVersion": synced_resource_version(
            meta.get("generation", 1), annotations, meta.get("labels"), scheme
        ),
        "conditions": [{"type": "Ready", "status": "True", "reason": "SecretSynced"}],
    }


if __name__ == "__main__":
    path = sys.argv[1]
    with open(path) as handle:
        obj = json.load(handle)
    obj["status"] = synced_status(obj, sys.argv[2] if len(sys.argv) > 2 else "eso")
    with open(path, "w") as handle:
        json.dump(obj, handle)
