# OpenBao AppRole rotation metadata, schema v1

This contract defines the non-secret record read by the `dmf-promsd` observer
and written by the rotation runner. It implements the metadata fields in the
accepted umbrella lifecycle plan. The record is one KV v2 value at:

```text
secret/data/platform/approle-rotation/<target>
```

The KV v2 API response wraps the record at `data.data`. `<target>` is a stable
registry identifier. The initial runtime targets are `external-secrets`,
`object-storage-openbao-snapshots`, and `object-storage-app-backups`. The
setup-only `born-inventory-netbox-reader` target has no rotation metadata and
is excluded from observer configuration.

All timestamps are integer Unix UTC seconds. `ttl_seconds` is a positive
integer. `target_version` is a positive monotonic integer for that target.
`schema_version` is the integer `1`.

The record contains:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer | Record schema, currently `1`. |
| `target` | string | Stable target ID matching the KV path suffix. |
| `approle_name` | string | OpenBao AppRole name. |
| `target_version` | integer | Monotonic version for this target. |
| `secret_id_accessor` | string | Sensitive operational metadata used by the runner for revocation. |
| `issued_at` | integer | SecretID issuance time in Unix seconds. |
| `expires_at` | integer | SecretID expiry time in Unix seconds. |
| `ttl_seconds` | integer | SecretID TTL in seconds. |
| `last_successful_rotation_at` | integer or null | Most recent completed rotation time. |
| `last_failure_at` | integer or null | Most recent failed run time. |
| `last_failure_reason_code` | string or null | Bounded reason code; paired with `last_failure_at`. |
| `latest_run_kind` | `scheduled` or `manual` | Origin of the latest logical run. |
| `latest_run_outcome` | `success` or `failure` | Sanitized result of the latest logical run. |
| `latest_run_at` | integer | Latest logical run time in Unix seconds. |
| `actor` | string | C5 actor for the latest run. |
| `role` | string | C5 authorization role for the latest run. |
| `request_id` | string | Stable non-secret C5 correlation ID for the latest run. |
| `reason` | string | Non-secret C5 reason for the latest run. |

`last_failure_at` and `last_failure_reason_code` are both null or both
populated. The accepted reason codes are `create_failed`, `lookup_failed`,
`delivery_failed`, `readiness_failed`, `cleanup_failed`, `metadata_failed`,
`lease_conflict`, and `invalid_state`. An unknown code is exported as
`unknown`; free-form exception text is never stored in this field.

The observer has read capability only on
`secret/data/platform/approle-rotation/*`. It reads configured target paths
directly and does not list the KV prefix. A missing, malformed, or unsupported
record is a collection failure, never a successful or zero-valued expiry.

The accessor is never returned, logged, or emitted as a metric. The C5 fields
are retained in the record but are not emitted as metric labels. The schema
does not contain a SecretID, token, staging payload, or application credential.

`tests/fixtures/openbao-approle-rotation-metadata-v1.json` is a synthetic
example. Its accessor and attribution values are placeholders, not credentials
or operator identities.
