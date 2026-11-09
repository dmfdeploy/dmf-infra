"""The synced resource version that the refresh probe computes must equal the value that
ESO v2.3.0 wrote. tests/fixtures/eso-synced-resource-version-live.json holds the
generation, annotations, labels and status.syncedResourceVersion of two ExternalSecrets
of a running ESO, one with no annotations and one with a force-sync annotation. The test
renders the expression in the probe's task file and the fake kubectl's helper against
both. It needs the Python of an Ansible install."""

import json
import sys
import unittest
from pathlib import Path

import yaml
from ansible.template import Templar, trust_as_template

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/fixtures"))
import eso_sync_version  # noqa: E402

VECTORS = json.loads((ROOT / "tests/fixtures/eso-synced-resource-version-live.json").read_text(encoding="utf-8"))
PROBE = yaml.safe_load((ROOT / "playbooks/tasks/verify-main-store-refresh.yml").read_text(encoding="utf-8"))[0]["vars"]


def probe_version(generation, annotations, labels):
    metadata = {"generation": generation}
    if annotations is not None:
        metadata["annotations"] = annotations
    if labels is not None:
        metadata["labels"] = labels
    templar = Templar(variables={"_verify_es_object": {"metadata": metadata}})
    return templar.template(trust_as_template(PROBE["_verify_es_version"]))


def hides_marker(annotations, labels=None):
    metadata = {"generation": 1, "annotations": annotations}
    if labels is not None:
        metadata["labels"] = labels
    templar = Templar(variables={"_verify_es_object": {"metadata": metadata}})
    return str(templar.template(trust_as_template(PROBE["_verify_es_metadata_hides_marker"]))).strip() == "True"


EMBEDDED = {"a": "x force-sync:verify-123-1"}
SEPARATE = {"a": "x", "force-sync": "verify-123-1"}
EMBEDDED_IN_KEY = {"a:v force-sync": "verify-123-1"}
SEPARATE_AFTER_KEY = {"a": "v", "force-sync": "verify-123-1"}


class SyncedResourceVersionTest(unittest.TestCase):
    def test_the_vectors_cover_an_object_with_and_without_annotations(self):
        self.assertEqual([bool(v["annotations"]) for v in VECTORS], [False, True])

    def test_the_probe_expression_equals_the_version_that_eso_wrote(self):
        for vector in VECTORS:
            with self.subTest(annotations=vector["annotations"]):
                self.assertEqual(
                    probe_version(vector["generation"], vector["annotations"], vector["labels"]),
                    vector["syncedResourceVersion"],
                )

    def test_the_fake_kubectl_helper_equals_the_version_that_eso_wrote(self):
        for vector in VECTORS:
            with self.subTest(annotations=vector["annotations"]):
                self.assertEqual(
                    eso_sync_version.synced_resource_version(
                        vector["generation"], vector["annotations"], vector["labels"]
                    ),
                    vector["syncedResourceVersion"],
                )

    def test_keys_are_hashed_in_byte_order_whatever_the_order_they_arrive_in(self):
        annotations = {"b": "2", "B": "1", "a": "3"}
        reordered = {"a": "3", "B": "1", "b": "2"}
        self.assertEqual(probe_version(1, annotations, None), probe_version(1, reordered, None))
        self.assertEqual(
            probe_version(1, annotations, {"l": "v"}),
            eso_sync_version.synced_resource_version(1, annotations, {"l": "v"}),
        )

    def test_a_different_annotation_gives_a_different_version(self):
        self.assertNotEqual(probe_version(1, {"force-sync": "x"}, None), probe_version(1, {"force-sync": "y"}, None))


class MetadataThatHidesTheMarkerTest(unittest.TestCase):
    def test_go_renders_different_maps_as_the_same_text(self):
        # This is why the probe refuses such metadata: the version is the same.
        for first, second in [(EMBEDDED, SEPARATE), (EMBEDDED_IN_KEY, SEPARATE_AFTER_KEY)]:
            with self.subTest(first=first):
                self.assertNotEqual(first, second)
                self.assertEqual(eso_sync_version._go_map(first), eso_sync_version._go_map(second))
                self.assertEqual(probe_version(1, first, None), probe_version(1, second, None))

    def test_the_guard_refuses_an_annotation_value_or_key_that_holds_the_marker_text(self):
        self.assertTrue(hides_marker(EMBEDDED))
        self.assertTrue(hides_marker(EMBEDDED_IN_KEY))

    def test_the_guard_refuses_a_label_that_holds_the_marker_text(self):
        self.assertTrue(hides_marker({}, {"l": "v force-sync:verify-123-1"}))
        self.assertTrue(hides_marker({}, {"force-sync": "verify-123-1"}))

    def test_the_guard_accepts_the_force_sync_annotation_itself_and_other_metadata(self):
        self.assertFalse(hides_marker(SEPARATE_AFTER_KEY))
        self.assertFalse(hides_marker({"force-sync": "verify-123-1"}))
        self.assertFalse(hides_marker({}))
        self.assertFalse(hides_marker({"a": "x"}, {"l": "v"}))

    def test_the_guard_handles_metadata_without_annotations_or_labels(self):
        templar = Templar(variables={"_verify_es_object": {"metadata": {"generation": 1}}})
        result = templar.template(trust_as_template(PROBE["_verify_es_metadata_hides_marker"]))
        self.assertEqual(str(result).strip(), "False")


if __name__ == "__main__":
    unittest.main()
