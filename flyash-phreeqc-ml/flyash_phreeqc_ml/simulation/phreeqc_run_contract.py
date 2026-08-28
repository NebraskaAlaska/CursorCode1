"""Authoritative PHREEQC review, confirmation, and execution-readiness contract.

Preview generation remains available for incomplete or unsupported scientific states.  Real
execution is narrower: only a scientifically runnable preview may be reviewed, the reviewed text
is snapshotted, explicit confirmation produces an immutable execution snapshot, and the executor
must verify that the live preview still has the same deterministic identity.

This module performs no execution and imports no UI or AI code.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import phreeqc_input_builder as builder
from . import source_terms

RUN_MODE_MATERIAL_RELEASE = "material_release"
RUN_MODE_MEASURED_LIQUID = "measured_liquid_speciation"

_RUNNABLE_SOURCE_STATUSES = {
    source_terms.STATUS_RELEASE_INCLUDED: RUN_MODE_MATERIAL_RELEASE,
    source_terms.STATUS_MEASURED_LIQUID: RUN_MODE_MEASURED_LIQUID,
}


class RunContractError(ValueError):
    """A preview cannot advance through review/confirmation."""


ENVIRONMENT_IDENTITY_VERSION = 1


@dataclass(frozen=True)
class FileIdentity:
    """Content identity plus diagnostic metadata for one execution-environment file."""

    resolved_path: str
    sha256: str
    size_bytes: int
    mtime_ns: int
    mode: int

    def identity_payload(self) -> dict:
        """Stable fields that define file identity; timestamps/mode remain diagnostic only."""
        return {
            "resolved_path": self.resolved_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }

    def to_dict(self) -> dict:
        return {
            **self.identity_payload(),
            "mtime_ns": self.mtime_ns,
            "mode": self.mode,
        }


@dataclass(frozen=True)
class ExecutionEnvironmentIdentity:
    """Immutable identity of the exact PHREEQC executable and thermodynamic database."""

    executable: FileIdentity
    database: FileIdentity
    version: int = ENVIRONMENT_IDENTITY_VERSION

    @property
    def identity_hash(self) -> str:
        payload = {
            "version": self.version,
            "executable": self.executable.identity_payload(),
            "database": self.database.identity_payload(),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "identity_hash": self.identity_hash,
            "executable": self.executable.to_dict(),
            "database": self.database.to_dict(),
        }


@dataclass(frozen=True)
class ReviewedPhreeqcInput:
    """Immutable copy of the exact preview text the user reviewed."""

    scenario_id: str
    phreeqc_input_text: str
    input_hash: str
    preview_status: str
    template_type: str
    run_mode: str
    source_term_status: str
    execution_basename: str | None = None


@dataclass(frozen=True)
class ConfirmedPhreeqcInput:
    """Immutable reviewed snapshot after a separate explicit confirmation action."""

    scenario_id: str
    phreeqc_input_text: str
    input_hash: str
    preview_status: str
    template_type: str
    run_mode: str
    source_term_status: str
    execution_environment: ExecutionEnvironmentIdentity
    execution_basename: str | None = None


@dataclass
class RunReadiness:
    """Deterministic, actionable readiness result shared by UI, policy, and executor."""

    scientific_ready: bool = False
    configuration_ready: bool = False
    reviewed: bool = False
    confirmed: bool = False
    snapshot_matches: bool = False
    environment_matches: bool = False
    run_mode: str | None = None
    input_hash: str | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def can_execute(self) -> bool:
        return (self.scientific_ready and self.configuration_ready and self.reviewed
                and self.confirmed and self.snapshot_matches and self.environment_matches)

    @property
    def message(self) -> str:
        return self.reasons[0] if self.reasons else "PHREEQC input is ready to execute."


def hash_input_text(text: str) -> str:
    """Stable identity for the exact UTF-8 PHREEQC text."""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_identity(path) -> FileIdentity:
    """Resolve and hash one exact regular file, raising ``RunContractError`` if unavailable."""
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        if not resolved.is_file():
            raise OSError("not a regular file")
        before = resolved.stat()
        digest = _sha256_file(resolved)
        stat = resolved.stat()
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode")
        if any(getattr(before, key) != getattr(stat, key) for key in stable_fields):
            raise OSError("file changed while its identity was being computed")
        return FileIdentity(
            resolved_path=str(resolved),
            sha256=digest,
            size_bytes=int(stat.st_size),
            mtime_ns=int(stat.st_mtime_ns),
            mode=int(stat.st_mode),
        )
    except (OSError, TypeError, ValueError) as exc:
        raise RunContractError(f"Cannot identify execution-environment file {path!r}: {exc}") from exc


def build_execution_environment(executable_path, database_path) -> ExecutionEnvironmentIdentity:
    """Mechanically identify the exact executable and database files available for a run."""
    return ExecutionEnvironmentIdentity(
        executable=file_identity(executable_path),
        database=file_identity(database_path),
    )


def optional_file_identity_hash(path) -> str | None:
    """Content/path identity for cache invalidation, or ``None`` when no file is available."""
    if not path:
        return None
    try:
        identity = file_identity(path)
    except RunContractError:
        return None
    payload = json.dumps(identity.identity_payload(), sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def preview_input_hash(preview) -> str:
    return hash_input_text(getattr(preview, "phreeqc_input_text", ""))


def preview_set_hash(previews) -> str:
    """Stable ordered identity for a reviewed sweep/search preview collection."""
    joined = "\n".join(
        f"{getattr(p, 'scenario_id', '')}:{preview_input_hash(p)}" for p in (previews or []))
    return hash_input_text(joined)


def generated_text_preview(generated_input, *, scenario_id: str | None = None):
    """Adapt only a typed output issued by the deterministic legacy builder.

    Trust is never inferred from PHREEQC text.  Raw text, even text containing every historical
    marker, is rejected.  The retained bytes, scenario identity, and basename all come directly
    from the immutable :class:`phreeqc_runner.GeneratedInput` object.
    """
    # Runtime import avoids a module cycle: the legacy runner imports this contract only inside
    # its review/execution functions.
    from .. import phreeqc_runner as legacy_runner

    if not legacy_runner.is_trusted_generated_input(generated_input):
        raise RunContractError(
            "Legacy execution review requires an intact GeneratedInput returned by the "
            "deterministic condition builder; arbitrary or modified PHREEQC text is "
            "preview-only and cannot execute.")
    trusted_sid = str(generated_input.scenario_id or generated_input.basename)
    if scenario_id is not None and str(scenario_id) != trusted_sid:
        raise RunContractError(
            "The requested scenario identifier does not match the deterministic generated input.")
    return builder.PhreeqcInputPreview(
        scenario_id=trusted_sid, phreeqc_input_text=generated_input.pqi_text,
        template_type=builder.TEMPLATE_NAOH,
        status=builder.STATUS_READY,
        includes_source_terms=True, source_term_mode=source_terms.MODE_GLOBAL,
        source_term_status=source_terms.STATUS_RELEASE_INCLUDED,
        warnings=[],
        assumptions=["legacy condition runner: configured assumed stock source term is explicit "
                     "in the reviewed input"],
        execution_basename=generated_input.basename,
    )


def assess_preview(preview) -> RunReadiness:
    """Assess the scientific portion of the runnable contract; configuration is separate."""
    result = RunReadiness()
    if preview is None:
        result.reasons.append("Build and review a PHREEQC input preview first.")
        return result

    text = getattr(preview, "phreeqc_input_text", "")
    result.input_hash = preview_input_hash(preview)
    source_status = getattr(preview, "source_term_status", None)
    result.run_mode = _RUNNABLE_SOURCE_STATUSES.get(source_status)

    if not text:
        result.reasons.append("The PHREEQC preview contains no input text.")
    if getattr(preview, "template_type", None) != builder.TEMPLATE_NAOH:
        result.reasons.append(
            "Execution currently supports the NaOH workflow only; keep this input as a "
            "preview/template and do not execute it.")
    if getattr(preview, "status", None) != builder.STATUS_READY:
        result.reasons.append(
            f"The preview is '{getattr(preview, 'status', 'unknown')}', not "
            f"'{builder.STATUS_READY}'; resolve its scientific warnings before execution.")
    if result.run_mode is None:
        result.reasons.append(
            "Choose an explicit, usable material release/source term or the separately "
            "supported measured-liquid speciation mode before execution.")

    result.scientific_ready = not result.reasons
    return result


def review_preview(preview) -> ReviewedPhreeqcInput:
    """Snapshot a scientifically runnable preview at the user's review step."""
    readiness = assess_preview(preview)
    if not readiness.scientific_ready:
        raise RunContractError(readiness.message)
    return ReviewedPhreeqcInput(
        scenario_id=str(getattr(preview, "scenario_id", "SIM")),
        phreeqc_input_text=str(preview.phreeqc_input_text),
        input_hash=str(readiness.input_hash),
        preview_status=str(preview.status),
        template_type=str(preview.template_type),
        run_mode=str(readiness.run_mode),
        source_term_status=str(preview.source_term_status),
        execution_basename=getattr(preview, "execution_basename", None),
    )


def confirm_reviewed(reviewed, execution_environment) -> ConfirmedPhreeqcInput:
    """Bind an intact reviewed snapshot to the exact available execution environment."""
    if not isinstance(reviewed, ReviewedPhreeqcInput):
        raise RunContractError("Review the exact PHREEQC input before confirming execution.")
    if hash_input_text(reviewed.phreeqc_input_text) != reviewed.input_hash:
        raise RunContractError("The reviewed PHREEQC snapshot changed; review it again.")
    if not isinstance(execution_environment, ExecutionEnvironmentIdentity):
        raise RunContractError(
            "Execution confirmation requires an available PHREEQC executable and database "
            "identity; configure both, then confirm again.")
    return ConfirmedPhreeqcInput(
        scenario_id=reviewed.scenario_id,
        phreeqc_input_text=reviewed.phreeqc_input_text,
        input_hash=reviewed.input_hash,
        preview_status=reviewed.preview_status,
        template_type=reviewed.template_type,
        run_mode=reviewed.run_mode,
        source_term_status=reviewed.source_term_status,
        execution_environment=execution_environment,
        execution_basename=reviewed.execution_basename,
    )


def assess_execution(preview, confirmation, availability) -> RunReadiness:
    """Evaluate the complete gate, including configuration and snapshot linkage."""
    result = assess_preview(preview)
    result.configuration_ready = bool(
        availability is not None and getattr(availability, "can_run", False))
    if not result.configuration_ready:
        result.reasons.append(
            getattr(availability, "message", None)
            or "PHREEQC executable and database configuration are required for execution.")

    if not isinstance(confirmation, ConfirmedPhreeqcInput):
        result.reasons.append(
            "Explicit confirmation of the reviewed PHREEQC input is required before execution.")
        return result

    result.reviewed = True
    result.confirmed = True
    confirmation_intact = (
        hash_input_text(confirmation.phreeqc_input_text) == confirmation.input_hash)
    result.snapshot_matches = bool(
        confirmation_intact and result.input_hash == confirmation.input_hash
        and str(getattr(preview, "scenario_id", "SIM")) == confirmation.scenario_id
        and getattr(preview, "execution_basename", None) == confirmation.execution_basename)
    if not confirmation_intact:
        result.reasons.append(
            "The confirmed PHREEQC snapshot changed; execution was blocked.")
    elif not result.snapshot_matches:
        result.reasons.append(
            "The PHREEQC input changed after review; review and confirm the new preview.")

    current_environment = getattr(availability, "environment_identity", None)
    result.environment_matches = bool(
        isinstance(current_environment, ExecutionEnvironmentIdentity)
        and current_environment.identity_hash == confirmation.execution_environment.identity_hash)
    if result.configuration_ready and not result.environment_matches:
        result.reasons.append(
            "The PHREEQC executable or thermodynamic database changed after confirmation; "
            "review the environment identity and confirm execution again.")
    return result
