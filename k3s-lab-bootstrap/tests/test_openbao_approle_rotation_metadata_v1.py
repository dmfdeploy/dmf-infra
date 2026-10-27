"""Structural checks for the shared OpenBao rotation metadata fixture."""

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/openbao-approle-rotation-metadata-v1.json"
TARGETS = {
    "external-secrets": "external-secrets",
    "object-storage-openbao-snapshots": "object-storage-openbao-snapshots",
    "object-storage-app-backups": "object-storage-app-backups",
}
REASON_CODES = {
    "create_failed",
    "lookup_failed",
    "delivery_failed",
    "readiness_failed",
    "cleanup_failed",
    "metadata_failed",
    "lease_conflict",
    "invalid_state",
}
FIELDS = {
    "schema_version",
    "target",
    "approle_name",
    "target_version",
    "secret_id_accessor",
    "issued_at",
    "expires_at",
    "ttl_seconds",
    "last_successful_rotation_at",
    "last_failure_at",
    "last_failure_reason_code",
    "latest_run_kind",
    "latest_run_outcome",
    "latest_run_at",
    "actor",
    "role",
    "request_id",
    "reason",
}


def validate_record(record):
    """Validate the v1 invariants needed by the read-only metric projection."""
    if not isinstance(record, dict) or set(record) != FIELDS:
        return False
    if record["schema_version"] != 1 or isinstance(record["schema_version"], bool):
        return False
    target = record["target"]
    if target not in TARGETS or record["approle_name"] != TARGETS[target]:
        return False
    for key in ("target_version", "issued_at", "expires_at", "ttl_seconds", "latest_run_at"):
        value = record[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return False
    if record["target_version"] < 1 or record["ttl_seconds"] < 1:
        return False
    for key in ("last_successful_rotation_at", "last_failure_at"):
        value = record[key]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            return False
    failure_time = record["last_failure_at"]
    failure_code = record["last_failure_reason_code"]
    if (failure_time is None) != (failure_code is None):
        return False
    if failure_code is not None and failure_code not in REASON_CODES:
        return False
    if record["latest_run_kind"] not in {"scheduled", "manual"}:
        return False
    if record["latest_run_outcome"] not in {"success", "failure"}:
        return False
    if record["latest_run_outcome"] == "failure" and failure_time != record["latest_run_at"]:
        return False
    for key in ("secret_id_accessor", "actor", "role", "request_id", "reason"):
        if not isinstance(record[key], str) or not record[key]:
            return False
    return record["expires_at"] == record["issued_at"] + record["ttl_seconds"]


class OpenBaoRotationMetadataV1Tests(unittest.TestCase):
    def test_synthetic_success_fixture_obeys_schema(self):
        record = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertTrue(validate_record(record))

    def test_unknown_or_setup_only_targets_are_not_exported(self):
        record = json.loads(FIXTURE.read_text(encoding="utf-8"))
        record["target"] = "born-inventory-netbox-reader"
        record["approle_name"] = "born-inventory-netbox-reader"
        self.assertFalse(validate_record(record))
        record["target"] = "unregistered-target"
        record["approle_name"] = "unregistered-role"
        self.assertFalse(validate_record(record))

    def test_unbounded_failure_text_and_mismatched_failure_fields_are_refused(self):
        record = json.loads(FIXTURE.read_text(encoding="utf-8"))
        record["last_failure_at"] = 1700000001
        self.assertFalse(validate_record(record))
        record["last_failure_reason_code"] = "api error included secret token"
        record["latest_run_kind"] = "scheduled"
        record["latest_run_outcome"] = "failure"
        record["latest_run_at"] = 1700000001
        self.assertFalse(validate_record(record))

    def test_malformed_time_types_or_accessor_omission_fail_closed(self):
        record = json.loads(FIXTURE.read_text(encoding="utf-8"))
        record["expires_at"] = "1702592000"
        self.assertFalse(validate_record(record))
        record = json.loads(FIXTURE.read_text(encoding="utf-8"))
        record.pop("secret_id_accessor")
        self.assertFalse(validate_record(record))


if __name__ == "__main__":
    unittest.main()
