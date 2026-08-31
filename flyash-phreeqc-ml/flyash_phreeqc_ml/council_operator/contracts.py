"""Closed request, configuration, policy, state, and approval contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import tomllib
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlparse


OPERATOR_VERSION = "1.0.0"
REQUEST_SCHEMA = "wpi-council-task-request/v1"
STATE_SCHEMA = "wpi-council-task-state/v1"
APPROVAL_SCHEMA = "wpi-council-approval/v1"
TASK_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{2,62}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40,64}\Z")
WORKER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
SHELL_OPERATORS = {";", "|", "||", "&", "&&", ">", ">>", "<", "<<"}
PUBLIC_CLASSIFICATIONS = {"public", "sanitized"}
STATES = (
    "draft",
    "submitted",
    "claimed",
    "routing",
    "planning",
    "coding",
    "coder_gate",
    "testing",
    "tester_gate",
    "reviewing",
    "correction_required",
    "automatic_gates_passed",
    "task_branch_pushed",
    "awaiting_human_review",
    "approved",
    "rejected",
    "expired",
    "failed",
    "cancelled",
)
TERMINAL_STATES = {"approved", "rejected", "failed", "cancelled"}
TRANSITIONS: dict[str, set[str]] = {
    "draft": {"submitted", "cancelled"},
    "submitted": {"claimed", "cancelled", "rejected"},
    "claimed": {"routing", "submitted", "expired", "failed", "cancelled"},
    "routing": {"planning", "correction_required", "expired", "failed", "cancelled"},
    "planning": {"coding", "correction_required", "expired", "failed", "cancelled"},
    "coding": {"coder_gate", "correction_required", "expired", "failed", "cancelled"},
    "coder_gate": {"testing", "correction_required", "expired", "failed", "cancelled"},
    "testing": {"tester_gate", "correction_required", "expired", "failed", "cancelled"},
    "tester_gate": {"reviewing", "correction_required", "expired", "failed", "cancelled"},
    "reviewing": {"correction_required", "automatic_gates_passed", "expired", "failed", "cancelled"},
    "correction_required": {"routing", "coding", "expired", "failed", "cancelled"},
    "automatic_gates_passed": {"task_branch_pushed", "expired", "failed", "cancelled"},
    "task_branch_pushed": {"awaiting_human_review", "failed"},
    "awaiting_human_review": {"approved", "rejected", "cancelled"},
    "expired": {"claimed", "cancelled", "rejected"},
    "approved": set(),
    "rejected": set(),
    "failed": set(),
    "cancelled": set(),
}


class ContractError(ValueError):
    """A closed operator contract failed validation."""


class SensitiveContentError(ContractError):
    """Potential secret or private research content was detected."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str, label: str) -> dt.datetime:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be a timestamp string")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{label} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{label} must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def canonical_json(value: Any) -> bytes:
    try:
        return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractError("value is not canonical JSON") from exc


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json_bytes(data: bytes, label: str) -> dict[str, Any]:
    if len(data) > 2 * 1024 * 1024:
        raise ContractError(f"{label} is too large")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be a JSON object")
    _reject_nonfinite(value)
    return value


def _reject_nonfinite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractError("NaN and Infinity are prohibited")
    if isinstance(value, dict):
        for item in value.values():
            _reject_nonfinite(item)
    elif isinstance(value, list):
        for item in value:
            _reject_nonfinite(item)


SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)\b(?:password|passwd|api[_ -]?key|access[_ -]?token|secret)\s*[:=]\s*\S+"),
)
SECRET_FIELD_RE = re.compile(r"(?i)(?:password|passwd|secret|token|api[_-]?key|cookie|credential|private[_-]?key)")
RESEARCH_DATA_RE = re.compile(
    r"(?i)(?:raw[_ /-]?data|unpublished|confidential experiment|measured sample|participant data|"
    r"patient data|data/raw/|experiments/.+/(?:data|outputs)/|\.xlsx\b|\.xls\b)"
)


def scan_public_text(value: Any, *, allow_research_data: bool = False) -> None:
    """Fail without echoing a possible secret value."""

    def walk(item: Any, key: str = "") -> Iterable[tuple[str, str]]:
        if isinstance(item, Mapping):
            for child_key, child in item.items():
                if isinstance(child_key, str) and SECRET_FIELD_RE.search(child_key):
                    raise SensitiveContentError("secret-like request fields are prohibited")
                yield from walk(child, str(child_key))
        elif isinstance(item, list):
            for child in item:
                yield from walk(child, key)
        elif isinstance(item, str):
            yield key, item

    for _key, text in walk(value):
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            raise SensitiveContentError("request contains prohibited secret-like content")
        if not allow_research_data and _key != "forbidden_paths" and RESEARCH_DATA_RE.search(text):
            raise SensitiveContentError("request appears to contain or request private research data")


def validate_relative_path(value: str, *, allow_glob: bool = True, allow_git: bool = False) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ContractError("repository paths must be non-empty POSIX strings")
    if value.startswith("/") or re.match(r"^[A-Za-z]:/", value):
        raise ContractError("absolute repository paths are prohibited")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ContractError("repository path traversal is prohibited")
    if not allow_glob and any(character in value for character in "*?["):
        raise ContractError("globs are not allowed in this path")
    if not allow_git and ".git" in pure.parts:
        raise ContractError(".git paths are prohibited")
    return pure.as_posix()


def _string_list(value: Any, label: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ContractError(f"{label} must be a list of non-empty strings")
    if nonempty and not value:
        raise ContractError(f"{label} must not be empty")
    return tuple(item.strip() for item in value)


def _command_list(value: Any) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list) or not value:
        raise ContractError("required_test_commands must be a non-empty list")
    commands: list[tuple[str, ...]] = []
    for command in value:
        if not isinstance(command, list) or not command or any(not isinstance(arg, str) or not arg for arg in command):
            raise ContractError("each test command must be a non-empty argument array")
        if any(any(character in arg for character in "\x00\n\r") or arg in SHELL_OPERATORS for arg in command):
            raise ContractError("test commands may not contain shell control operators")
        commands.append(tuple(command))
    return tuple(commands)


REQUEST_FIELDS = {
    "schema_version",
    "revision",
    "task_id",
    "title",
    "goal",
    "background",
    "repository",
    "base_branch",
    "base_commit",
    "allowed_paths",
    "forbidden_paths",
    "acceptance_criteria",
    "required_test_commands",
    "scientific_risk",
    "security_privacy",
    "permitted_external_resources",
    "max_correction_rounds",
    "max_duration_seconds",
    "max_changed_files",
    "max_patch_bytes",
    "requested_backend",
    "requester_identity",
    "created_at",
    "canonical_request_hash",
    "issue_references",
    "review_notes",
    "relevant_documentation",
    "resource_update_request_reference",
}


@dataclass(frozen=True)
class TaskRequest:
    schema_version: str
    revision: int
    task_id: str
    title: str
    goal: str
    background: str
    repository: str
    base_branch: str
    base_commit: str
    allowed_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    required_test_commands: tuple[tuple[str, ...], ...]
    scientific_risk: str
    security_privacy: str
    permitted_external_resources: tuple[str, ...]
    max_correction_rounds: int
    max_duration_seconds: int
    max_changed_files: int
    max_patch_bytes: int
    requested_backend: str
    requester_identity: str
    created_at: str
    canonical_request_hash: str
    issue_references: tuple[str, ...] = ()
    review_notes: str = ""
    relevant_documentation: tuple[str, ...] = ()
    resource_update_request_reference: str = ""

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "TaskRequest":
        if set(value) != REQUEST_FIELDS:
            unknown = sorted(set(value) - REQUEST_FIELDS)
            missing = sorted(REQUEST_FIELDS - set(value))
            detail = "unknown fields" if unknown else "missing fields"
            raise ContractError(f"request has {detail}")
        if value["schema_version"] != REQUEST_SCHEMA:
            raise ContractError("unsupported request schema")
        revision = value["revision"]
        if type(revision) is not int or revision < 1:
            raise ContractError("request revision must be a positive integer")
        task_id = value["task_id"]
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            raise ContractError("unsafe task ID")
        strings = {}
        for key in ("title", "goal", "background", "repository", "base_branch", "base_commit", "requester_identity", "created_at"):
            item = value[key]
            if not isinstance(item, str) or not item.strip():
                raise ContractError(f"{key} must be a non-empty string")
            strings[key] = item.strip()
        if not COMMIT_RE.fullmatch(strings["base_commit"]):
            raise ContractError("base_commit must be a full hexadecimal commit")
        if strings["base_branch"].startswith("-") or ".." in strings["base_branch"] or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", strings["base_branch"]):
            raise ContractError("unsafe base branch")
        parse_time(strings["created_at"], "created_at")
        allowed = tuple(validate_relative_path(item) for item in _string_list(value["allowed_paths"], "allowed_paths", nonempty=True))
        forbidden = tuple(validate_relative_path(item, allow_git=True) for item in _string_list(value["forbidden_paths"], "forbidden_paths"))
        criteria = _string_list(value["acceptance_criteria"], "acceptance_criteria", nonempty=True)
        commands = _command_list(value["required_test_commands"])
        scientific_risk = value["scientific_risk"]
        if scientific_risk not in {"low", "moderate", "high", "critical"}:
            raise ContractError("unknown scientific risk")
        privacy = value["security_privacy"]
        if privacy not in {"public", "sanitized", "private", "confidential"}:
            raise ContractError("unknown security/privacy classification")
        resources = _string_list(value["permitted_external_resources"], "permitted_external_resources")
        for resource in resources:
            parsed = urlparse(resource)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ContractError("external resources must be credential-free HTTPS URLs")
        limits: dict[str, int] = {}
        for key, minimum in (
            ("max_correction_rounds", 0),
            ("max_duration_seconds", 60),
            ("max_changed_files", 1),
            ("max_patch_bytes", 1),
        ):
            item = value[key]
            if type(item) is not int or item < minimum:
                raise ContractError(f"invalid {key}")
            limits[key] = item
        backend = value["requested_backend"]
        if backend not in {"hermes", "command", "fake"}:
            raise ContractError("unknown requested backend")
        optional_lists = {
            key: _string_list(value[key], key)
            for key in ("issue_references", "relevant_documentation")
        }
        for key in ("review_notes", "resource_update_request_reference"):
            if not isinstance(value[key], str):
                raise ContractError(f"{key} must be a string")
        supplied_hash = value["canonical_request_hash"]
        if not isinstance(supplied_hash, str) or (supplied_hash and not SHA256_RE.fullmatch(supplied_hash)):
            raise ContractError("canonical_request_hash must be SHA-256")
        scan_public_text(value, allow_research_data=privacy in {"private", "confidential"})
        instance = cls(
            schema_version=REQUEST_SCHEMA,
            revision=revision,
            task_id=task_id,
            title=strings["title"],
            goal=strings["goal"],
            background=strings["background"],
            repository=strings["repository"],
            base_branch=strings["base_branch"],
            base_commit=strings["base_commit"],
            allowed_paths=allowed,
            forbidden_paths=forbidden,
            acceptance_criteria=criteria,
            required_test_commands=commands,
            scientific_risk=scientific_risk,
            security_privacy=privacy,
            permitted_external_resources=resources,
            max_correction_rounds=limits["max_correction_rounds"],
            max_duration_seconds=limits["max_duration_seconds"],
            max_changed_files=limits["max_changed_files"],
            max_patch_bytes=limits["max_patch_bytes"],
            requested_backend=backend,
            requester_identity=strings["requester_identity"],
            created_at=strings["created_at"],
            canonical_request_hash=supplied_hash,
            issue_references=optional_lists["issue_references"],
            review_notes=value["review_notes"],
            relevant_documentation=optional_lists["relevant_documentation"],
            resource_update_request_reference=value["resource_update_request_reference"],
        )
        expected = instance.computed_hash()
        if verify_hash and supplied_hash != expected:
            raise ContractError("canonical request hash does not match")
        return instance

    @classmethod
    def seal(cls, value: Mapping[str, Any]) -> "TaskRequest":
        candidate = dict(value)
        candidate["canonical_request_hash"] = ""
        provisional = cls.from_dict(candidate, verify_hash=False)
        candidate["canonical_request_hash"] = provisional.computed_hash()
        return cls.from_dict(candidate)

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "task_id": self.task_id,
            "title": self.title,
            "goal": self.goal,
            "background": self.background,
            "repository": self.repository,
            "base_branch": self.base_branch,
            "base_commit": self.base_commit,
            "allowed_paths": list(self.allowed_paths),
            "forbidden_paths": list(self.forbidden_paths),
            "acceptance_criteria": list(self.acceptance_criteria),
            "required_test_commands": [list(command) for command in self.required_test_commands],
            "scientific_risk": self.scientific_risk,
            "security_privacy": self.security_privacy,
            "permitted_external_resources": list(self.permitted_external_resources),
            "max_correction_rounds": self.max_correction_rounds,
            "max_duration_seconds": self.max_duration_seconds,
            "max_changed_files": self.max_changed_files,
            "max_patch_bytes": self.max_patch_bytes,
            "requested_backend": self.requested_backend,
            "requester_identity": self.requester_identity,
            "created_at": self.created_at,
            "canonical_request_hash": self.canonical_request_hash if include_hash else "",
            "issue_references": list(self.issue_references),
            "review_notes": self.review_notes,
            "relevant_documentation": list(self.relevant_documentation),
            "resource_update_request_reference": self.resource_update_request_reference,
        }
        return result

    def computed_hash(self) -> str:
        return digest_json(self.to_dict(include_hash=False))


@dataclass(frozen=True)
class ProjectPolicy:
    project_slug: str
    allowed_repository: str
    public_repository: bool
    allowed_base_branches: tuple[str, ...]
    task_branch_prefix: str
    forbidden_branches: tuple[str, ...]
    default_test_commands: tuple[tuple[str, ...], ...]
    allowed_test_command_prefixes: tuple[tuple[str, ...], ...]
    scientific_risk_floor: str
    role_sequence: tuple[str, ...]
    max_correction_rounds: int
    max_task_duration_seconds: int
    max_patch_bytes: int
    max_changed_files: int
    max_role_invocations: int
    max_log_bytes: int
    lease_seconds: int
    allow_policy_expiry_takeover: bool
    forbidden_paths: tuple[str, ...]
    required_human_approvals: tuple[str, ...]
    resource_steward_automatic_actions: tuple[str, ...]
    resource_steward_human_actions: tuple[str, ...]
    obsidian_handoff_note: str
    council_required_sha256: Mapping[str, str]

    @classmethod
    def load(cls, path: Path) -> "ProjectPolicy":
        try:
            value = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ContractError("project policy is unreadable or invalid TOML") from exc
        expected = {
            "project", "git", "tests", "limits", "security", "approvals",
            "resource_steward", "obsidian", "council_contracts",
        }
        if set(value) != expected:
            raise ContractError("project policy has unknown or missing sections")
        project, git, tests, limits = value["project"], value["git"], value["tests"], value["limits"]
        security, approvals = value["security"], value["approvals"]
        steward, obsidian, council = value["resource_steward"], value["obsidian"], value["council_contracts"]
        instance = cls(
            project_slug=str(project["slug"]),
            allowed_repository=str(project["allowed_repository"]),
            public_repository=project["public_repository"],
            allowed_base_branches=tuple(git["allowed_base_branches"]),
            task_branch_prefix=str(git["task_branch_prefix"]),
            forbidden_branches=tuple(git["forbidden_branches"]),
            default_test_commands=tuple(tuple(item) for item in tests["default_commands"]),
            allowed_test_command_prefixes=tuple(tuple(item) for item in tests["allowed_command_prefixes"]),
            scientific_risk_floor=str(security["scientific_risk_floor"]),
            role_sequence=tuple(project["role_sequence"]),
            max_correction_rounds=limits["max_correction_rounds"],
            max_task_duration_seconds=limits["max_task_duration_seconds"],
            max_patch_bytes=limits["max_patch_bytes"],
            max_changed_files=limits["max_changed_files"],
            max_role_invocations=limits["max_role_invocations"],
            max_log_bytes=limits["max_log_bytes"],
            lease_seconds=limits["lease_seconds"],
            allow_policy_expiry_takeover=limits["allow_policy_expiry_takeover"],
            forbidden_paths=tuple(validate_relative_path(item, allow_git=True) for item in security["forbidden_paths"]),
            required_human_approvals=tuple(approvals["required"]),
            resource_steward_automatic_actions=tuple(steward["automatic_actions"]),
            resource_steward_human_actions=tuple(steward["human_actions"]),
            obsidian_handoff_note=validate_relative_path(str(obsidian["handoff_note"]), allow_glob=False),
            council_required_sha256=dict(council["required_sha256"]),
        )
        instance.validate()
        return instance

    def validate(self) -> None:
        if not TASK_ID_RE.fullmatch(self.project_slug):
            raise ContractError("unsafe project slug")
        if type(self.public_repository) is not bool:
            raise ContractError("public_repository must be a boolean")
        if self.role_sequence != ("planner", "coder", "tester", "reviewer"):
            raise ContractError("role sequence must preserve the Council contract")
        if not self.task_branch_prefix.endswith("/") or self.task_branch_prefix.startswith("/"):
            raise ContractError("task branch prefix is unsafe")
        if {"main", "master"}.isdisjoint(set(self.forbidden_branches)):
            raise ContractError("main and master must be forbidden")
        if any(not SHA256_RE.fullmatch(value) for value in self.council_required_sha256.values()):
            raise ContractError("Council contract hashes must be SHA-256")
        for item in (
            self.max_task_duration_seconds,
            self.max_patch_bytes,
            self.max_changed_files,
            self.max_role_invocations,
            self.max_log_bytes,
            self.lease_seconds,
        ):
            if type(item) is not int or item < 1:
                raise ContractError("policy limits must be positive integers")
        if type(self.max_correction_rounds) is not int or self.max_correction_rounds < 0:
            raise ContractError("max correction rounds is invalid")
        if type(self.allow_policy_expiry_takeover) is not bool:
            raise ContractError("expiry takeover policy must be boolean")
        allowed = set(self.resource_steward_automatic_actions)
        required = {"check", "show", "download-candidate", "verify-candidate", "build-candidate", "test-candidate", "compare", "export-report"}
        if allowed != required or {"promote", "rollback"} & allowed:
            raise ContractError("Resource Steward automatic action policy is unsafe")
        if set(self.resource_steward_human_actions) != {"promote", "rollback"}:
            raise ContractError("Resource Steward human action policy is incomplete")

    def validate_request(self, request: TaskRequest, *, has_private_control_remote: bool) -> None:
        if request.repository != self.allowed_repository:
            raise ContractError("request repository is not the allowed project repository")
        if request.base_branch not in self.allowed_base_branches:
            raise ContractError("request base branch is forbidden")
        if request.base_branch in {"main", "master"}:
            raise ContractError("direct main/master work is prohibited")
        for path in request.allowed_paths:
            if any(_patterns_intersect(path, forbidden) for forbidden in (*self.forbidden_paths, *request.forbidden_paths)):
                raise ContractError("allowed paths intersect a forbidden path")
        if request.max_correction_rounds > self.max_correction_rounds:
            raise ContractError("request correction limit exceeds policy")
        if request.max_duration_seconds > self.max_task_duration_seconds:
            raise ContractError("request duration exceeds policy")
        if request.max_changed_files > self.max_changed_files or request.max_patch_bytes > self.max_patch_bytes:
            raise ContractError("request patch limits exceed policy")
        for command in request.required_test_commands:
            if not any(command[: len(prefix)] == prefix for prefix in self.allowed_test_command_prefixes):
                raise ContractError("request contains an unapproved test command")
        if self.public_repository and not has_private_control_remote and request.security_privacy not in PUBLIC_CLASSIFICATIONS:
            raise SensitiveContentError("private/confidential tasks require a separate private control repository")
        scan_public_text(request.to_dict(), allow_research_data=False)
        lower = " ".join((request.goal, request.background, *request.acceptance_criteria)).lower()
        prohibited_intents = (
            "push main", "push to main", "force push", "force-push", "bypass tests",
            "skip tests", "deploy", "change repository visibility", "branch protection",
        )
        if any(intent in lower for intent in prohibited_intents):
            raise ContractError("request contains a prohibited control-plane instruction")


def _path_matches(path: str, pattern: str) -> bool:
    import fnmatch

    return path == pattern or fnmatch.fnmatchcase(path, pattern) or (
        pattern.endswith("/**") and (path == pattern[:-3] or path.startswith(pattern[:-2]))
    )


def _patterns_intersect(left: str, right: str) -> bool:
    if _path_matches(left, right) or _path_matches(right, left):
        return True
    left_prefix = re.split(r"[*?\[]", left, maxsplit=1)[0].rstrip("/")
    right_prefix = re.split(r"[*?\[]", right, maxsplit=1)[0].rstrip("/")
    return bool(left_prefix and right_prefix) and (
        left_prefix == right_prefix
        or left_prefix.startswith(right_prefix + "/")
        or right_prefix.startswith(left_prefix + "/")
    )


@dataclass(frozen=True)
class OperatorConfig:
    repository_path: Path
    code_remote: str
    control_remote: str
    worker_id: str
    sandbox_root: Path
    state_cache_root: Path
    council_root: Path | None
    hermes_executable: Path | None
    obsidian_vault: Path | None
    control_remote_private: bool = False
    command_profiles: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    command_executable_sha256: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "OperatorConfig":
        try:
            value = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ContractError("local operator config is unreadable or invalid TOML") from exc
        if set(value) - {"operator", "command_backend"}:
            raise ContractError("local config has unknown sections")
        operator = value.get("operator")
        if not isinstance(operator, dict):
            raise ContractError("local config is missing [operator]")
        required = {"repository_path", "code_remote", "control_remote", "worker_id", "sandbox_root", "state_cache_root"}
        if not required.issubset(operator) or set(operator) - (required | {"council_root", "hermes_executable", "obsidian_vault", "control_remote_private"}):
            raise ContractError("local operator config has unknown or missing fields")
        def optional_path(key: str) -> Path | None:
            item = operator.get(key, "")
            return Path(item).expanduser() if item else None
        command = value.get("command_backend", {})
        profiles = {
            role: tuple(arguments)
            for role, arguments in command.get("profiles", {}).items()
        }
        hashes = dict(command.get("executable_sha256", {}))
        def configured(key: str, environment_key: str) -> str:
            return os.environ.get(environment_key, str(operator[key]))
        def configured_optional(key: str, environment_key: str) -> Path | None:
            item = os.environ.get(environment_key, str(operator.get(key, "")))
            return Path(item).expanduser() if item else None
        control_private: Any = operator.get("control_remote_private", False)
        if "WPI_COUNCIL_CONTROL_REPO_PRIVATE" in os.environ:
            raw_private = os.environ["WPI_COUNCIL_CONTROL_REPO_PRIVATE"].strip().lower()
            if raw_private not in {"true", "false"}:
                raise ContractError("WPI_COUNCIL_CONTROL_REPO_PRIVATE must be true or false")
            control_private = raw_private == "true"
        instance = cls(
            repository_path=Path(configured("repository_path", "WPI_COUNCIL_REPOSITORY")).expanduser(),
            code_remote=configured("code_remote", "WPI_COUNCIL_CODE_REPO"),
            control_remote=configured("control_remote", "WPI_COUNCIL_CONTROL_REPO"),
            worker_id=configured("worker_id", "WPI_COUNCIL_WORKER_ID"),
            sandbox_root=Path(configured("sandbox_root", "WPI_COUNCIL_SANDBOX_ROOT")).expanduser(),
            state_cache_root=Path(configured("state_cache_root", "WPI_COUNCIL_STATE_CACHE_ROOT")).expanduser(),
            council_root=configured_optional("council_root", "WPI_AI_COUNCIL_ROOT"),
            hermes_executable=configured_optional("hermes_executable", "WPI_HERMES_EXECUTABLE"),
            obsidian_vault=configured_optional("obsidian_vault", "WPI_OBSIDIAN_VAULT"),
            control_remote_private=control_private,
            command_profiles=profiles,
            command_executable_sha256=hashes,
        )
        instance.validate()
        return instance

    def validate(self) -> None:
        if not WORKER_ID_RE.fullmatch(self.worker_id):
            raise ContractError("unsafe worker identity")
        if not self.code_remote or not self.control_remote:
            raise ContractError("code and control remotes are required")
        if type(self.control_remote_private) is not bool:
            raise ContractError("control_remote_private must be a boolean")
        if self.control_remote_private and self.control_remote == self.code_remote:
            raise ContractError("the public code remote cannot be attested as a private control remote")
        for path in (self.repository_path, self.sandbox_root, self.state_cache_root):
            if not path.is_absolute():
                raise ContractError("operator paths must be absolute")
        if self.council_root is not None and not self.council_root.is_absolute():
            raise ContractError("Council root must be absolute")
        if self.hermes_executable is not None and not self.hermes_executable.is_absolute():
            raise ContractError("Hermes executable must be absolute")
        for role, arguments in self.command_profiles.items():
            if role not in {"planner", "coder", "tester", "reviewer"} or not arguments:
                raise ContractError("invalid command backend role profile")
            if any(not item or any(char in item for char in "\x00\n\r") or item in SHELL_OPERATORS for item in arguments):
                raise ContractError("unsafe command backend argument array")
        for value in self.command_executable_sha256.values():
            if not SHA256_RE.fullmatch(value):
                raise ContractError("command executable identity must be SHA-256")

    @property
    def has_private_control_remote(self) -> bool:
        return self.control_remote != self.code_remote and self.control_remote_private


def load_request_file(path: Path) -> TaskRequest:
    data = path.read_bytes()
    if path.suffix.lower() == ".md":
        try:
            text = data.decode("utf-8")
        except UnicodeError as exc:
            raise ContractError("request Markdown must be UTF-8") from exc
        match = re.fullmatch(r"# WPI Council Request\s*\n\s*```json\s*\n(?P<json>.*?)\n```\s*", text, re.DOTALL)
        if not match:
            raise ContractError("Markdown request must contain only the canonical JSON envelope")
        data = match.group("json").encode("utf-8")
    return TaskRequest.from_dict(load_json_bytes(data, "task request"))


def render_request_markdown(request: TaskRequest) -> str:
    payload = json.dumps(request.to_dict(), indent=2, sort_keys=True, allow_nan=False)
    return f"# WPI Council Request\n\n```json\n{payload}\n```\n"


def new_state_record(request: TaskRequest, project_slug: str, actor: str) -> dict[str, Any]:
    now = utc_now()
    record = {
        "schema_version": STATE_SCHEMA,
        "project_slug": project_slug,
        "task_id": request.task_id,
        "request": request.to_dict(),
        "request_hash": request.canonical_request_hash,
        "base_commit": request.base_commit,
        "state": "submitted",
        "revision": 1,
        "worker_id": None,
        "lock": None,
        "transitions": [
            {"sequence": 1, "from": "draft", "to": "submitted", "event": "submit", "actor": actor, "at": now, "detail": ""}
        ],
        "evidence": {"attempts": [], "summary": {}, "retained_failed_attempts": []},
        "approvals": [],
        "task_branch": None,
        "final_task_commit": None,
        "updated_at": now,
        "operator_version": OPERATOR_VERSION,
        "failure": None,
        "correction_round": 0,
    }
    validate_state_record(record)
    return record


STATE_FIELDS = {
    "schema_version", "project_slug", "task_id", "request", "request_hash", "base_commit",
    "state", "revision", "worker_id", "lock", "transitions", "evidence", "approvals",
    "task_branch", "final_task_commit", "updated_at", "operator_version", "failure",
    "correction_round",
}


def validate_state_record(record: Mapping[str, Any]) -> None:
    if set(record) != STATE_FIELDS or record.get("schema_version") != STATE_SCHEMA:
        raise ContractError("invalid task state schema")
    if not TASK_ID_RE.fullmatch(str(record.get("task_id", ""))):
        raise ContractError("invalid task state identity")
    request = TaskRequest.from_dict(record["request"])
    if request.task_id != record["task_id"] or request.canonical_request_hash != record["request_hash"]:
        raise ContractError("task state request binding is invalid")
    if request.base_commit != record["base_commit"]:
        raise ContractError("task state base binding is invalid")
    if record["state"] not in STATES:
        raise ContractError("unknown task state")
    if type(record["revision"]) is not int or record["revision"] < 1:
        raise ContractError("invalid task state revision")
    if type(record["correction_round"]) is not int or record["correction_round"] < 0:
        raise ContractError("invalid correction round")
    parse_time(record["updated_at"], "state updated_at")
    transitions = record["transitions"]
    if not isinstance(transitions, list) or not transitions:
        raise ContractError("state transition history is missing")
    previous = "draft"
    for index, item in enumerate(transitions, 1):
        if not isinstance(item, dict) or set(item) != {"sequence", "from", "to", "event", "actor", "at", "detail"}:
            raise ContractError("invalid state transition entry")
        if any(not isinstance(item[key], str) for key in ("from", "to", "event", "actor", "at", "detail")):
            raise ContractError("invalid state transition value")
        if item["sequence"] != index or item["from"] != previous or item["to"] not in TRANSITIONS[previous]:
            raise ContractError("invalid state transition history")
        parse_time(item["at"], "transition timestamp")
        previous = item["to"]
    if previous != record["state"]:
        raise ContractError("state does not match transition history")
    lock = record["lock"]
    if lock is not None:
        expected = {"task_id", "request_hash", "base_commit", "worker_id", "acquired_at", "heartbeat_at", "lease_expires_at", "operator_version", "state_revision"}
        if not isinstance(lock, dict) or set(lock) != expected:
            raise ContractError("invalid task lock schema")
        if lock["task_id"] != record["task_id"] or lock["request_hash"] != record["request_hash"] or lock["base_commit"] != record["base_commit"]:
            raise ContractError("task lock binding is invalid")
        if lock["worker_id"] != record["worker_id"] or not WORKER_ID_RE.fullmatch(str(lock["worker_id"])):
            raise ContractError("task lock worker is invalid")
        for key in ("acquired_at", "heartbeat_at", "lease_expires_at"):
            parse_time(lock[key], key)
    if not isinstance(record["evidence"], dict) or set(record["evidence"]) != {"attempts", "summary", "retained_failed_attempts"}:
        raise ContractError("invalid evidence envelope")
    attempts = record["evidence"]["attempts"]
    attempt_fields = {
        "attempt_id", "passed", "correction_kind", "correction_detail", "route",
        "changed_paths", "patch_hash", "test_evidence_hash", "reviewer_report_hash",
        "reviewer_verdict", "evidence",
    }
    if not isinstance(attempts, list) or not isinstance(record["evidence"]["summary"], dict):
        raise ContractError("invalid attempt or summary evidence")
    for attempt in attempts:
        if not isinstance(attempt, dict) or set(attempt) != attempt_fields:
            raise ContractError("invalid attempt evidence schema")
        if not re.fullmatch(r"attempt-\d{2}", str(attempt["attempt_id"])) or type(attempt["passed"]) is not bool:
            raise ContractError("invalid attempt identity or result")
        if attempt["correction_kind"] is not None and not isinstance(attempt["correction_kind"], str):
            raise ContractError("invalid attempt correction kind")
        if not isinstance(attempt["changed_paths"], list) or not isinstance(attempt["route"], dict) or not isinstance(attempt["evidence"], dict):
            raise ContractError("invalid attempt evidence value")
        if any(not isinstance(attempt[key], str) for key in ("correction_detail", "patch_hash", "test_evidence_hash", "reviewer_report_hash", "reviewer_verdict")):
            raise ContractError("invalid attempt hash or verdict value")
    retained = record["evidence"]["retained_failed_attempts"]
    if not isinstance(retained, list) or any(not re.fullmatch(r"attempt-\d{2}", str(item)) for item in retained):
        raise ContractError("invalid retained-attempt evidence")
    if not isinstance(record["approvals"], list):
        raise ContractError("invalid approval history")
    for approval in record["approvals"]:
        validate_approval_artifact(approval)
    scan_public_text(record["evidence"], allow_research_data=False)


def transition_record(record: Mapping[str, Any], target: str, *, actor: str, event: str, detail: str = "", mutate: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    validate_state_record(record)
    current = str(record["state"])
    if current in TERMINAL_STATES:
        raise ContractError("terminal task evidence is immutable")
    if target not in TRANSITIONS[current]:
        raise ContractError(f"invalid state transition: {current} -> {target}")
    updated = json.loads(json.dumps(record))
    updated["state"] = target
    updated["revision"] += 1
    updated["updated_at"] = utc_now()
    updated["transitions"].append(
        {
            "sequence": len(updated["transitions"]) + 1,
            "from": current,
            "to": target,
            "event": event,
            "actor": actor,
            "at": updated["updated_at"],
            "detail": detail[:512],
        }
    )
    if mutate:
        mutate(updated)
    validate_state_record(updated)
    return updated


APPROVAL_BINDING_FIELDS = {
    "task_id", "request_hash", "base_commit", "final_task_commit",
    "patch_hash", "test_evidence_hash", "reviewer_report_hash",
}
STALE_LOCK_BINDING_FIELDS = APPROVAL_BINDING_FIELDS | {
    "expired_state_sha256", "expired_state_revision", "previous_worker_id",
    "lease_expires_at", "expired_at", "approved_at", "approved_by",
}


def _approval_bindings(record: Mapping[str, Any]) -> dict[str, str]:
    summary = record["evidence"]["summary"]
    return {
        "task_id": record["task_id"],
        "request_hash": record["request_hash"],
        "base_commit": record["base_commit"],
        "final_task_commit": record["final_task_commit"] or "",
        "patch_hash": str(summary.get("patch_hash", "")),
        "test_evidence_hash": str(summary.get("test_evidence_hash", "")),
        "reviewer_report_hash": str(summary.get("reviewer_report_hash", "")),
    }


def canonical_expired_state_sha256(record: Mapping[str, Any]) -> str:
    """Return the deterministic identity of one explicitly expired state.

    The complete closed state record is hashed.  A heartbeat, evidence update,
    timestamp edit, revision change, approval, or transition therefore produces
    a different identity and invalidates an approval created for an earlier
    observation.
    """

    validate_state_record(record)
    if record["state"] != "expired" or record["lock"] is None or record["worker_id"] is None:
        raise ContractError("stale-lock approval requires an explicitly expired locked state")
    transition = record["transitions"][-1]
    if (
        transition["to"] != "expired"
        or transition["event"] != "lease_expired"
    ):
        raise ContractError("expired state does not have exact lease-expiry provenance")
    if parse_time(transition["at"], "expired_at") < parse_time(
        record["lock"]["lease_expires_at"], "lease_expires_at"
    ):
        raise ContractError("expired state predates its exact lease expiry")
    return digest_json(record)


def _stale_lock_bindings(
    record: Mapping[str, Any], *, approved_at: str, approved_by: str
) -> dict[str, str]:
    bindings = _approval_bindings(record)
    bindings.update(
        {
            "expired_state_sha256": canonical_expired_state_sha256(record),
            "expired_state_revision": str(record["revision"]),
            "previous_worker_id": str(record["worker_id"]),
            "lease_expires_at": str(record["lock"]["lease_expires_at"]),
            "expired_at": str(record["transitions"][-1]["at"]),
            "approved_at": approved_at,
            "approved_by": approved_by,
        }
    )
    return bindings


def approval_artifact(kind: str, record: Mapping[str, Any], *, approved_by: str, extra_bindings: Mapping[str, str] | None = None) -> dict[str, Any]:
    validate_state_record(record)
    allowed = {"task_branch", "pull_request", "merge", "deployment", "resource_promotion", "resource_rollback", "stale_lock_takeover"}
    if kind not in allowed:
        raise ContractError("unknown approval kind")
    if not approved_by.startswith("human:") or len(approved_by) <= len("human:"):
        raise ContractError("approval must be attributed to a human")
    approved_at = utc_now()
    if kind == "stale_lock_takeover":
        if extra_bindings:
            raise ContractError("stale-lock bindings are derived only from the exact expired state")
        bindings = _stale_lock_bindings(record, approved_at=approved_at, approved_by=approved_by)
    else:
        bindings = _approval_bindings(record)
        if extra_bindings:
            bindings.update(extra_bindings)
    artifact = {
        "schema_version": APPROVAL_SCHEMA,
        "kind": kind,
        "approved_by": approved_by,
        "approved_at": approved_at,
        "bindings": bindings,
        "approval_hash": "",
    }
    artifact["approval_hash"] = digest_json(
        {
            "schema_version": APPROVAL_SCHEMA,
            "kind": kind,
            "approved_by": approved_by,
            "approved_at": approved_at,
            "bindings": bindings,
        }
    )
    validate_approval_artifact(artifact)
    return artifact


def validate_approval_artifact(artifact: Mapping[str, Any]) -> None:
    fields = {"schema_version", "kind", "approved_by", "approved_at", "bindings", "approval_hash"}
    if not isinstance(artifact, Mapping) or set(artifact) != fields:
        raise ContractError("invalid approval schema")
    allowed = {"task_branch", "pull_request", "merge", "deployment", "resource_promotion", "resource_rollback", "stale_lock_takeover"}
    if artifact.get("schema_version") != APPROVAL_SCHEMA or artifact.get("kind") not in allowed:
        raise ContractError("invalid approval kind or version")
    approved_by = artifact.get("approved_by")
    if not isinstance(approved_by, str) or not approved_by.startswith("human:") or len(approved_by) <= len("human:"):
        raise ContractError("approval must be attributed to a human")
    parse_time(artifact.get("approved_at"), "approval approved_at")
    bindings = artifact.get("bindings")
    if not isinstance(bindings, Mapping) or not APPROVAL_BINDING_FIELDS.issubset(bindings):
        raise ContractError("approval bindings are incomplete")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in bindings.items()):
        raise ContractError("approval bindings must be strings")
    if artifact["kind"] == "stale_lock_takeover":
        if set(bindings) != STALE_LOCK_BINDING_FIELDS:
            raise ContractError("stale-lock approval bindings are incomplete")
        if not SHA256_RE.fullmatch(bindings["expired_state_sha256"]):
            raise ContractError("stale-lock expired-state hash is invalid")
        if not re.fullmatch(r"[1-9][0-9]*", bindings["expired_state_revision"]):
            raise ContractError("stale-lock expired-state revision is invalid")
        if not WORKER_ID_RE.fullmatch(bindings["previous_worker_id"]):
            raise ContractError("stale-lock previous worker identity is invalid")
        parse_time(bindings["lease_expires_at"], "stale-lock lease_expires_at")
        parse_time(bindings["expired_at"], "stale-lock expired_at")
        if bindings["approved_at"] != artifact["approved_at"] or bindings["approved_by"] != approved_by:
            raise ContractError("stale-lock approval attribution binding is invalid")
    expected = digest_json(
        {
            "schema_version": APPROVAL_SCHEMA,
            "kind": artifact["kind"],
            "approved_by": approved_by,
            "approved_at": artifact["approved_at"],
            "bindings": dict(bindings),
        }
    )
    if artifact.get("approval_hash") != expected:
        raise ContractError("approval hash is invalid")


def validate_stale_takeover_approval(
    artifact: Mapping[str, Any],
    record: Mapping[str, Any],
    *,
    now: dt.datetime | None = None,
) -> None:
    """Validate one takeover approval against the exact current remote state."""

    validate_approval_artifact(artifact)
    validate_state_record(record)
    if artifact.get("kind") != "stale_lock_takeover":
        raise ContractError("stale-lock takeover requires the dedicated approval kind")
    if any(item.get("approval_hash") == artifact["approval_hash"] for item in record["approvals"]):
        raise ContractError("stale-lock approval has already been consumed")
    approved_at = parse_time(artifact["approved_at"], "approval approved_at")
    expected = _stale_lock_bindings(
        record,
        approved_at=artifact["approved_at"],
        approved_by=artifact["approved_by"],
    )
    if dict(artifact["bindings"]) != expected:
        raise ContractError("stale-lock approval does not match the exact expired state")
    expired_at = parse_time(record["transitions"][-1]["at"], "expired_at")
    lease_expires_at = parse_time(record["lock"]["lease_expires_at"], "lease_expires_at")
    if approved_at < expired_at or approved_at < lease_expires_at:
        raise ContractError("stale-lock approval predates the current expiry")
    current = now or dt.datetime.now(dt.timezone.utc)
    if approved_at > current.astimezone(dt.timezone.utc):
        raise ContractError("stale-lock approval time is in the future")


def command_text(arguments: Iterable[str]) -> str:
    return shlex.join(list(arguments))
