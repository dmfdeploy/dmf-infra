"""Registry model: load the registry, validate its shape, derive thresholds.

The registry holds non-secret metadata only. This module never reads or
returns a credential.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1
METADATA_PREFIX = "secret/data/platform/approle-rotation/"
DESTINATION_KINDS = {
    "kubernetes_secret_key": ("namespace", "name", "key"),
    "openbao_kv2_record": ("path", "key"),
}
GATE_KINDS = {
    "ClusterSecretStore": ("name", "condition"),
    "ExternalSecret": ("name_pattern", "condition"),
    "KubernetesSecret": ("namespace", "name", "required_key"),
}
# An ExternalSecret gate names its namespaces in one of these ways.
EXTERNAL_SECRET_NAMESPACE_FIELDS = ("namespaces_from", "namespace")
# Only a gate of this kind shows that a consumer refreshed after a replacement.
ACCEPTANCE_GATE_KINDS = {"ExternalSecret"}
DISPOSITION_EVIDENCE = ("issuance_verification", "revocation_verification")
# In this step, no status can make a target accepted. Step 3 of the credential
# lifecycle plan defines the proof that can add a status here.
PROOF_STATUSES = {"missing"}
GATE_STATUSES = {"existing_check", "required_rotation_gate"}
_TTL = re.compile(r"([1-9][0-9]*)h")


class RegistryError(ValueError):
    """The registry is malformed. The message lists every problem found."""


def ttl_seconds(text):
    match = _TTL.fullmatch(text) if isinstance(text, str) else None
    if not match:
        raise RegistryError(f"finite SecretID TTL is missing or malformed: {text!r}")
    return int(match.group(1)) * 3600


@dataclass(frozen=True)
class Thresholds:
    """Seconds for one target. Rotation is due `rotation_due_after` seconds
    after issue. The alerts fire when the time left drops to the warning and
    critical amounts."""

    ttl: int
    rotation_due_after: int
    warning_remaining: int
    critical_remaining: int


def thresholds(entry):
    ttl = ttl_seconds(entry["secret_id_ttl"])
    return Thresholds(
        ttl=ttl,
        rotation_due_after=int(ttl * entry["rotation_due_fraction"]),
        warning_remaining=int(ttl * entry["warning_remaining_fraction"]),
        critical_remaining=int(ttl * entry["critical_remaining_fraction"]),
    )


def _fraction_errors(target, entry):
    errors = []
    values = {}
    for name in ("rotation_due_fraction", "warning_remaining_fraction", "critical_remaining_fraction"):
        value = entry.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 1:
            errors.append(f"{target}: {name} must be a number between 0 and 1")
        else:
            values[name] = value
    if len(values) == 3 and not values["critical_remaining_fraction"] < values["warning_remaining_fraction"]:
        errors.append(f"{target}: the critical fraction must be smaller than the warning fraction")
    return errors


def _file_errors(root, label, relative):
    """Report a named file that does not exist under `root`."""
    if root is not None and not (Path(root) / relative).is_file():
        return [f"{label}: {relative} does not exist"]
    return []


def validate(registry, root=None):
    """Return a list of problems, empty when the registry is well formed.

    With `root` (the k3s-lab-bootstrap directory), every file the registry
    names must exist."""
    errors = []
    if registry.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    runtime = registry.get("runtime_targets", {})
    dispositions = registry.get("dispositions", {})
    for overlap in sorted(set(runtime) & set(dispositions)):
        errors.append(f"{overlap}: appears as both runtime and setup-only")
    for target, entry in runtime.items():
        if entry.get("approle_name") != target:
            errors.append(f"{target}: target and AppRole name differ")
        try:
            ttl_seconds(entry.get("secret_id_ttl"))
        except RegistryError as error:
            errors.append(f"{target}: {error}")
        errors.extend(_fraction_errors(target, entry))
        if not isinstance(entry.get("consumer_type"), str) or not entry.get("consumer_type"):
            errors.append(f"{target}: consumer_type is missing")
        if not entry.get("create_site"):
            errors.append(f"{target}: create_site is missing")
        else:
            errors.extend(_file_errors(root, f"{target} create_site", entry["create_site"]))
        if entry.get("metadata_path") != METADATA_PREFIX + target:
            errors.append(f"{target}: metadata_path must be {METADATA_PREFIX}{target}")
        destinations = entry.get("destinations") or []
        if not destinations:
            errors.append(f"{target}: destination evidence is missing")
        for destination in destinations:
            kind = destination.get("kind")
            required = DESTINATION_KINDS.get(kind)
            if required is None:
                errors.append(f"{target}: unsupported destination kind {kind!r}")
            elif any(not destination.get(field) for field in required):
                errors.append(f"{target}: destination {kind} needs {', '.join(required)}")
        gates = entry.get("readiness_gates") or []
        if not gates:
            errors.append(f"{target}: readiness evidence is missing")
        elif not any(gate.get("kind") in ACCEPTANCE_GATE_KINDS for gate in gates):
            errors.append(
                f"{target}: no readiness gate shows that a consumer refreshed after a replacement; "
                "declare an ExternalSecret gate"
            )
        elif not any(gate.get("kind") == "ClusterSecretStore" for gate in gates):
            errors.append(f"{target}: an ExternalSecret gate needs the target's ClusterSecretStore gate to name the expected store")
        proof = entry.get("fresh_authentication_proof")
        if (
            not isinstance(proof, dict)
            or proof.get("status") not in PROOF_STATUSES
            or not proof.get("evidence_gap")
        ):
            errors.append(
                f"{target}: fresh_authentication_proof needs the status 'missing' and an evidence_gap; "
                "step 3 of the credential lifecycle plan defines the proof that can change it"
            )
        for gate in gates:
            kind = gate.get("kind")
            required = GATE_KINDS.get(kind)
            if required is None:
                errors.append(f"{target}: unsupported readiness gate kind {kind!r}")
            elif any(not gate.get(field) for field in required):
                errors.append(f"{target}: readiness gate {kind} needs {', '.join(required)}")
            if kind == "ExternalSecret" and not any(
                gate.get(field) for field in EXTERNAL_SECRET_NAMESPACE_FIELDS
            ):
                errors.append(f"{target}: ExternalSecret gate needs namespaces_from or namespace")
            if gate.get("status") not in GATE_STATUSES:
                errors.append(f"{target}: readiness gate {kind} has an unknown status")
            if gate.get("status") == "required_rotation_gate" and not gate.get("evidence_gap"):
                errors.append(f"{target}: a new readiness gate is claimed without current evidence")
    for target, entry in dispositions.items():
        if entry.get("approle_name") != target:
            errors.append(f"{target}: disposition and AppRole name differ")
        try:
            ttl_seconds(entry.get("secret_id_ttl"))
        except RegistryError as error:
            errors.append(f"{target}: {error}")
        if entry.get("disposition") != "setup_only" or entry.get("continuous_rotation") is not False:
            errors.append(f"{target}: setup-only exclusion is not explicit")
        if not entry.get("create_site"):
            errors.append(f"{target}: create_site is missing")
        else:
            errors.extend(_file_errors(root, f"{target} create_site", entry["create_site"]))
        entrypoints = entry.get("entrypoints")
        if not isinstance(entrypoints, list) or not entrypoints or not all(isinstance(p, str) and p for p in entrypoints):
            errors.append(f"{target}: the checked Ansible entrypoints are missing")
        else:
            for entrypoint in entrypoints:
                errors.extend(_file_errors(root, f"{target} entrypoint", entrypoint))
        if entry.get("fresh_on_rerun_required") is not True:
            errors.append(f"{target}: the fresh-on-rerun requirement is missing")
        for field in DISPOSITION_EVIDENCE:
            proof = entry.get(field)
            if not isinstance(proof, dict) or not proof.get("requirement") or not proof.get("evidence"):
                errors.append(f"{target}: {field} needs a requirement and an evidence path")
            else:
                errors.extend(_file_errors(root, f"{target} {field}", proof["evidence"]))
    return errors


def load(path, root=None):
    """Load and validate the registry. Raise RegistryError on any problem."""
    with open(path, encoding="utf-8") as handle:
        registry = json.load(handle)
    errors = validate(registry, root)
    if errors:
        raise RegistryError("; ".join(errors))
    return registry
