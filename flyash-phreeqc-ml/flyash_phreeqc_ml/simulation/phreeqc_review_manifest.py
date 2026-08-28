"""Exact local review evidence for deterministic legacy PHREEQC input sets.

The manifest is deliberately not a scientific result.  It binds an ordered set of immutable,
builder-issued inputs to the source/design identity and the exact execution environment that a
human reviewed.  Verification is all-or-nothing and completes before a caller may run PHREEQC.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .. import phreeqc_runner
from . import phreeqc_run_contract

SCHEMA = "wpi.virtual-lab.phreeqc-exact-review"
VERSION = 1
EVIDENCE_TYPE = "local_review_evidence_not_scientific_result"
DEFAULT_MANIFEST_NAME = "PHREEQC_REVIEW_MANIFEST.json"


class ReviewManifestError(ValueError):
    """Preparation or exact manifest verification failed closed."""


@dataclass(frozen=True)
class VerifiedReviewSet:
    manifest_path: Path
    manifest_identity: str
    ordered_set_hash: str
    inputs: tuple
    execution_environment: phreeqc_run_contract.ExecutionEnvironmentIdentity


def _json_bytes(value) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReviewManifestError(f"Review identity is not canonical JSON: {exc}") from exc


def canonical_hash(value) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def bytes_hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def dataframe_hash(frame) -> str:
    """Stable identity for the exact ordered dataframe values used by a script."""
    if frame is None:
        return bytes_hash(b"<none>")
    text = frame.to_csv(index=False, lineterminator="\n")
    return bytes_hash(text.encode("utf-8"))


def _input_entry(generated_input, order: int) -> dict:
    if not phreeqc_runner.is_trusted_generated_input(generated_input):
        raise ReviewManifestError(
            "Every reviewed CLI input must be an intact deterministic GeneratedInput.")
    basename = str(generated_input.basename)
    return {
        "order": int(order),
        "scenario_id": str(generated_input.scenario_id),
        "basename": basename,
        "relative_path": f"{basename}.pqi",
        "input_sha256": bytes_hash(generated_input.pqi_text.encode("utf-8")),
    }


def _ordered_entries(inputs) -> list[dict]:
    entries = [_input_entry(item, i) for i, item in enumerate(inputs)]
    basenames = [entry["basename"] for entry in entries]
    scenarios = [entry["scenario_id"] for entry in entries]
    if len(set(basenames)) != len(basenames):
        raise ReviewManifestError("Generated input basenames must be unique within a review set.")
    if len(set(scenarios)) != len(scenarios):
        raise ReviewManifestError("Generated scenario identifiers must be unique within a review set.")
    return entries


def ordered_set_hash(entries) -> str:
    fields = [
        {key: entry[key] for key in
         ("order", "scenario_id", "basename", "relative_path", "input_sha256")}
        for entry in entries
    ]
    return canonical_hash(fields)


def _environment_payload_from_document(document: dict) -> dict:
    try:
        env = document["execution_environment"]
        return {
            "version": int(env["version"]),
            "executable": {
                "resolved_path": str(env["executable"]["resolved_path"]),
                "sha256": str(env["executable"]["sha256"]),
                "size_bytes": int(env["executable"]["size_bytes"]),
            },
            "database": {
                "resolved_path": str(env["database"]["resolved_path"]),
                "sha256": str(env["database"]["sha256"]),
                "size_bytes": int(env["database"]["size_bytes"]),
            },
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ReviewManifestError("Manifest execution-environment identity is incomplete.") from exc


def prepare_review_manifest(inputs, manifest_path, *, source_identity: dict,
                            execution_environment) -> Path:
    """Write exact UTF-8 inputs and a versioned manifest; never invokes PHREEQC."""
    if not isinstance(execution_environment,
                      phreeqc_run_contract.ExecutionEnvironmentIdentity):
        raise ReviewManifestError(
            "An available executable/database identity is required to prepare review evidence.")
    inputs = tuple(inputs or ())
    if not inputs:
        raise ReviewManifestError("No deterministic PHREEQC inputs were produced for review.")
    entries = _ordered_entries(inputs)
    manifest_path = Path(manifest_path).expanduser().resolve()
    review_dir = manifest_path.parent
    review_dir.mkdir(parents=True, exist_ok=True)
    existing_inputs = sorted(review_dir.glob("*.pqi"))
    if manifest_path.exists() or existing_inputs:
        raise ReviewManifestError(
            "Review destination is not empty; choose a new directory so stale inputs cannot be "
            "mistaken for this review set.")

    for entry, generated_input in zip(entries, inputs):
        (review_dir / entry["relative_path"]).write_bytes(
            generated_input.pqi_text.encode("utf-8"))

    source_identity = dict(source_identity or {})
    source_hash = canonical_hash(source_identity)
    environment_doc = execution_environment.to_dict()
    document = {
        "schema": SCHEMA,
        "version": VERSION,
        "evidence_type": EVIDENCE_TYPE,
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "source_identity": source_identity,
        "source_identity_hash": source_hash,
        "execution_environment": environment_doc,
        "ordered_set_hash": ordered_set_hash(entries),
        "inputs": entries,
    }
    document["manifest_identity"] = canonical_hash({
        key: document[key] for key in
        ("schema", "version", "evidence_type", "source_identity_hash",
         "execution_environment", "ordered_set_hash", "inputs")
    })
    manifest_path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    return manifest_path


def load_review_manifest(manifest_path) -> dict:
    path = Path(manifest_path).expanduser().resolve()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewManifestError(f"Cannot read review manifest {path}: {exc}") from exc
    if document.get("schema") != SCHEMA or document.get("version") != VERSION:
        raise ReviewManifestError(
            f"Unsupported review manifest schema/version; expected {SCHEMA} v{VERSION}.")
    if document.get("evidence_type") != EVIDENCE_TYPE:
        raise ReviewManifestError("Manifest is not labeled as local review evidence.")
    return document


def verify_review_manifest(manifest_path, inputs, *, source_identity: dict,
                           execution_environment) -> VerifiedReviewSet:
    """Fail closed unless source, environment, order, identities, files, and bytes all match."""
    if not isinstance(execution_environment,
                      phreeqc_run_contract.ExecutionEnvironmentIdentity):
        raise ReviewManifestError("The current execution environment is unavailable.")
    path = Path(manifest_path).expanduser().resolve()
    document = load_review_manifest(path)

    if document.get("source_identity_hash") != canonical_hash(document.get("source_identity", {})):
        raise ReviewManifestError("Manifest source identity is internally inconsistent.")
    current_source = dict(source_identity or {})
    if (document.get("source_identity") != current_source
            or document.get("source_identity_hash") != canonical_hash(current_source)):
        raise ReviewManifestError(
            "Source data or design identity changed after review; prepare a new manifest.")

    recorded_environment_hash = document.get("execution_environment", {}).get("identity_hash")
    if canonical_hash(_environment_payload_from_document(document)) != recorded_environment_hash:
        raise ReviewManifestError("Manifest execution-environment identity is internally inconsistent.")
    if recorded_environment_hash != execution_environment.identity_hash:
        raise ReviewManifestError(
            "The PHREEQC executable or database identity changed after review; prepare and "
            "review a new manifest.")

    candidates = tuple(inputs or ())
    expected_entries = _ordered_entries(candidates)
    recorded_entries = document.get("inputs")
    if recorded_entries != expected_entries:
        raise ReviewManifestError(
            "Generated inputs are stale, modified, missing, additional, reordered, or have a "
            "different scenario/basename identity.")
    expected_set_hash = ordered_set_hash(expected_entries)
    if document.get("ordered_set_hash") != expected_set_hash:
        raise ReviewManifestError("The ordered input-set hash does not match the reviewed set.")

    recorded_files = {entry["relative_path"] for entry in expected_entries}
    actual_files = {item.name for item in path.parent.glob("*.pqi") if item.is_file()}
    if actual_files != recorded_files:
        raise ReviewManifestError(
            "Reviewed input files are missing or additional .pqi files are present.")
    for entry, generated_input in zip(expected_entries, candidates):
        input_path = path.parent / entry["relative_path"]
        try:
            reviewed_bytes = input_path.read_bytes()
        except OSError as exc:
            raise ReviewManifestError(f"Cannot read reviewed input {input_path}: {exc}") from exc
        if (bytes_hash(reviewed_bytes) != entry["input_sha256"]
                or reviewed_bytes != generated_input.pqi_text.encode("utf-8")):
            raise ReviewManifestError(
                f"Reviewed input bytes changed for {entry['basename']}; prepare a new manifest.")

    manifest_identity = canonical_hash({
        key: document[key] for key in
        ("schema", "version", "evidence_type", "source_identity_hash",
         "execution_environment", "ordered_set_hash", "inputs")
    })
    if document.get("manifest_identity") != manifest_identity:
        raise ReviewManifestError("Manifest bound identity does not match its exact contents.")
    return VerifiedReviewSet(
        path, manifest_identity, expected_set_hash, candidates, execution_environment)
