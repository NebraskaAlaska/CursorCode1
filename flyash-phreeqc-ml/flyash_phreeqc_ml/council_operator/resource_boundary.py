"""Narrow adapter to the existing deterministic Phase 4 Resource Steward."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from .contracts import ContractError, ProjectPolicy, validate_approval_artifact
from .sandbox import bounded_process, safe_git_environment


class ResourceBoundaryError(RuntimeError):
    """A Resource Steward action crossed or failed its approval boundary."""


class ResourceStewardAdapter:
    def __init__(self, policy: ProjectPolicy, project_root: Path, store_root: Path):
        self.policy = policy
        self.project_root = project_root
        self.store_root = store_root

    def execute(
        self,
        action: str,
        arguments: Sequence[str],
        *,
        approval: Mapping[str, Any] | None = None,
        timeout_seconds: int = 3600,
    ) -> dict[str, Any]:
        if action in self.policy.resource_steward_human_actions:
            expected_kind = "resource_promotion" if action == "promote" else "resource_rollback"
            if not approval or approval.get("kind") != expected_kind:
                raise ResourceBoundaryError(f"Resource Steward {action} requires separate exact human approval")
            bindings = approval.get("bindings", {})
            if not bindings.get("proposal_id") or not bindings.get("candidate_sha256"):
                raise ResourceBoundaryError("resource approval is not bound to proposal and candidate identity")
            try:
                validate_approval_artifact(approval)
            except ContractError as exc:
                raise ResourceBoundaryError("resource approval hash or human attribution is invalid") from exc
            if approval.get("kind") != expected_kind:
                raise ResourceBoundaryError("resource approval hash or human attribution is invalid")
        elif action not in self.policy.resource_steward_automatic_actions:
            raise ResourceBoundaryError("Resource Steward action is not permitted by project policy")
        if any(not isinstance(item, str) or not item or "\x00" in item for item in arguments):
            raise ResourceBoundaryError("Resource Steward arguments are invalid")
        command = [
            sys.executable,
            "-m",
            "flyash_phreeqc_ml.resource_steward",
            "--store-root",
            str(self.store_root),
            action,
            *arguments,
        ]
        result = bounded_process(
            command,
            cwd=self.project_root,
            timeout_seconds=timeout_seconds,
            max_output_bytes=self.policy.max_log_bytes,
            environment=safe_git_environment(roles=True),
        )
        evidence = {
            "action": action,
            "exit_code": result["exit_code"],
            "timed_out": result["timed_out"],
            "stdout_sha256": result["stdout_sha256"],
            "stderr_sha256": result["stderr_sha256"],
            "approval_hash": approval.get("approval_hash") if approval else None,
            "active_resource_changed_automatically": False if action not in {"promote", "rollback"} else None,
        }
        if result["exit_code"] != 0:
            raise ResourceBoundaryError("Resource Steward action failed; bounded hashes were retained")
        return evidence
