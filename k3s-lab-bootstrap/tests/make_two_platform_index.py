#!/usr/bin/env python3
"""Write a tiny OCI image layout whose one tag is a two-platform index.

Usage: make_two_platform_index.py <layout-dir> <tag>

The fixture needs no network or container runtime. Each platform gets its own
config and layer, so the two platform manifests have different digests.
"""
import hashlib
import json
import os
import sys


def put(layout, data):
    digest = hashlib.sha256(data).hexdigest()
    path = os.path.join(layout, "blobs", "sha256", digest)
    with open(path, "wb") as f:
        f.write(data)
    return {"digest": "sha256:" + digest, "size": len(data)}


def main(layout, tag):
    os.makedirs(os.path.join(layout, "blobs", "sha256"))
    with open(os.path.join(layout, "oci-layout"), "w") as f:
        json.dump({"imageLayoutVersion": "1.0.0"}, f)

    entries = []
    for arch in ("amd64", "arm64"):
        config = put(
            os.path.join(layout),
            json.dumps({"architecture": arch, "os": "linux",
                        "rootfs": {"type": "layers", "diff_ids": []}},
                       sort_keys=True).encode(),
        )
        layer = put(layout, ("layer for " + arch + "\n").encode())
        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"mediaType": "application/vnd.oci.image.config.v1+json", **config},
            "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar", **layer}],
        }
        ref = put(layout, json.dumps(manifest, sort_keys=True).encode())
        entries.append({
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            **ref,
            "platform": {"architecture": arch, "os": "linux"},
        })

    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": entries,
    }
    top = put(layout, json.dumps(index, sort_keys=True).encode())
    with open(os.path.join(layout, "index.json"), "w") as f:
        json.dump({
            "schemaVersion": 2,
            "manifests": [{
                "mediaType": "application/vnd.oci.image.index.v1+json",
                **top,
                "annotations": {"org.opencontainers.image.ref.name": tag},
            }],
        }, f)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
