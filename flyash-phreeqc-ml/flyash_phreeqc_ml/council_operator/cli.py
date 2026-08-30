"""Command-line control plane for personal and Hermes computers."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sys
import time
from typing import Any, Sequence

from .contracts import (
    ContractError,
    REQUEST_FIELDS,
    REQUEST_SCHEMA,
    TaskRequest,
    load_json_bytes,
    render_request_markdown,
    utc_now,
)
from .operator import CouncilOperator, OperatorError
from .store import StateStoreError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = PROJECT_ROOT / "config" / "council_operator_policy.toml"
DEFAULT_CONFIG = Path.home() / ".config" / "wpi-virtual-lab" / "council.toml"


def _operator(arguments: argparse.Namespace) -> CouncilOperator:
    config = Path(arguments.config or os.environ.get("WPI_COUNCIL_CONFIG", DEFAULT_CONFIG)).expanduser()
    policy = Path(arguments.policy or DEFAULT_POLICY).expanduser()
    return CouncilOperator.load(config, policy)


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, allow_nan=False))


def _create_request(operator: CouncilOperator, arguments: argparse.Namespace) -> dict[str, Any]:
    branch = arguments.base_branch or operator.sandboxes.verify_live_checkout()["branch"]
    commit = arguments.base_commit or operator.sandboxes.verify_live_checkout()["head"]
    commands = arguments.test_command or [shlex.join(item) for item in operator.policy.default_test_commands]
    request = TaskRequest.seal(
        {
            "schema_version": REQUEST_SCHEMA,
            "revision": arguments.revision,
            "task_id": arguments.task_id,
            "title": arguments.title,
            "goal": arguments.goal,
            "background": arguments.background,
            "repository": operator.policy.allowed_repository,
            "base_branch": branch,
            "base_commit": commit,
            "allowed_paths": arguments.allowed_path,
            "forbidden_paths": list(operator.policy.forbidden_paths),
            "acceptance_criteria": arguments.acceptance,
            "required_test_commands": [shlex.split(item, posix=True) for item in commands],
            "scientific_risk": arguments.scientific_risk,
            "security_privacy": arguments.security_privacy,
            "permitted_external_resources": arguments.external_resource,
            "max_correction_rounds": min(arguments.max_correction_rounds, operator.policy.max_correction_rounds),
            "max_duration_seconds": min(arguments.max_duration_seconds, operator.policy.max_task_duration_seconds),
            "max_changed_files": min(arguments.max_changed_files, operator.policy.max_changed_files),
            "max_patch_bytes": min(arguments.max_patch_bytes, operator.policy.max_patch_bytes),
            "requested_backend": arguments.backend,
            "requester_identity": arguments.requester,
            "created_at": utc_now(),
            "canonical_request_hash": "",
            "issue_references": arguments.issue_reference,
            "review_notes": "",
            "relevant_documentation": arguments.documentation,
            "resource_update_request_reference": "",
        }
    )
    operator.policy.validate_request(request, has_private_control_remote=operator.config.has_private_control_remote)
    output = Path(arguments.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ContractError("request output already exists")
    output.write_text(
        render_request_markdown(request) if output.suffix.lower() == ".md" else json.dumps(request.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"request": str(output), "task_id": request.task_id, "request_hash": request.canonical_request_hash}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wpi-council")
    parser.add_argument("--config")
    parser.add_argument("--policy")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--worker", action="store_true")

    create = sub.add_parser("create-request")
    create.add_argument("output")
    create.add_argument("--task-id", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--goal", required=True)
    create.add_argument("--background", required=True)
    create.add_argument("--allowed-path", action="append", required=True)
    create.add_argument("--acceptance", action="append", required=True)
    create.add_argument("--test-command", action="append")
    create.add_argument("--base-branch")
    create.add_argument("--base-commit")
    create.add_argument("--revision", type=int, default=1)
    create.add_argument("--scientific-risk", choices=("low", "moderate", "high", "critical"), default="moderate")
    create.add_argument("--security-privacy", choices=("public", "sanitized", "private", "confidential"), default="sanitized")
    create.add_argument("--external-resource", action="append", default=[])
    create.add_argument("--backend", choices=("hermes", "command", "fake"), default="hermes")
    create.add_argument("--requester", required=True)
    create.add_argument("--issue-reference", action="append", default=[])
    create.add_argument("--documentation", action="append", default=[])
    create.add_argument("--max-correction-rounds", type=int, default=2)
    create.add_argument("--max-duration-seconds", type=int, default=28800)
    create.add_argument("--max-changed-files", type=int, default=24)
    create.add_argument("--max-patch-bytes", type=int, default=1048576)

    submit = sub.add_parser("submit")
    submit.add_argument("request")

    for name in ("status", "claim", "heartbeat", "resume", "release", "cancel", "expire"):
        command = sub.add_parser(name)
        command.add_argument("task_id")
    sub.choices["claim"].add_argument("--stale-approval")
    sub.choices["expire"].add_argument("--actor", required=True)
    watch = sub.add_parser("watch")
    watch.add_argument("task_id")
    watch.add_argument("--interval", type=float, default=5.0)
    watch.add_argument("--max-seconds", type=int, default=3600)

    run = sub.add_parser("run")
    run.add_argument("task_id")
    run.add_argument("--backend", choices=("hermes", "command", "fake"))
    run.add_argument("--overnight", action="store_true")
    run.add_argument("--fake-scenario", default="valid")
    run.add_argument("--no-push", action="store_true")

    review = sub.add_parser("review")
    review.add_argument("task_id")
    review.add_argument("--output")

    for command_name in (
        "approve-task-branch", "approve-pull-request", "approve-merge", "approve-deployment",
        "approve-resource-promotion", "approve-resource-rollback",
    ):
        command = sub.add_parser(command_name)
        command.add_argument("task_id")
        command.add_argument("--approved-by", required=True)
        if command_name.startswith("approve-resource"):
            command.add_argument("--proposal-id", required=True)
            command.add_argument("--candidate-sha256", required=True)
    reject = sub.add_parser("reject")
    reject.add_argument("task_id")
    reject.add_argument("--actor", required=True)
    reject.add_argument("--reason", required=True)
    cancel = sub.choices["cancel"]
    cancel.add_argument("--actor", required=True)

    stale = sub.add_parser("create-stale-lock-approval")
    stale.add_argument("task_id")
    stale.add_argument("--approved-by", required=True)
    stale.add_argument("--output", required=True)

    sync = sub.add_parser("sync-handoff")
    sync.add_argument("task_id")
    sync.add_argument("--expected-before-hash")
    return parser


APPROVAL_COMMANDS = {
    "approve-task-branch": "task_branch",
    "approve-pull-request": "pull_request",
    "approve-merge": "merge",
    "approve-deployment": "deployment",
    "approve-resource-promotion": "resource_promotion",
    "approve-resource-rollback": "resource_rollback",
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        operator = _operator(arguments)
        if arguments.command == "doctor":
            result = operator.doctor(worker=arguments.worker)
        elif arguments.command == "create-request":
            result = _create_request(operator, arguments)
        elif arguments.command == "submit":
            result = operator.submit(Path(arguments.request))
        elif arguments.command == "status":
            result = operator.status(arguments.task_id)
        elif arguments.command == "watch":
            deadline = time.monotonic() + arguments.max_seconds
            previous = None
            result = {}
            while time.monotonic() <= deadline:
                result = operator.status(arguments.task_id)
                summary = {"state": result["state"], "revision": result["revision"], "worker_id": result["worker_id"], "lease_stale": result["lease_stale"]}
                if summary != previous:
                    _print(summary)
                    previous = summary
                if result["state"] in {"awaiting_human_review", "approved", "rejected", "failed", "cancelled"}:
                    break
                time.sleep(max(0.2, arguments.interval))
            else:
                raise OperatorError("watch deadline reached")
            return 0
        elif arguments.command == "claim":
            override = None
            if arguments.stale_approval:
                override = load_json_bytes(Path(arguments.stale_approval).read_bytes(), "stale-lock approval")
            result = operator.claim(arguments.task_id, human_override=override)
        elif arguments.command == "heartbeat":
            result = operator.heartbeat(arguments.task_id)
        elif arguments.command == "run":
            result = operator.run(
                arguments.task_id,
                backend_name=arguments.backend,
                overnight=arguments.overnight,
                fake_scenario=arguments.fake_scenario,
                push_task_branch=not arguments.no_push,
            )
        elif arguments.command == "resume":
            result = operator.resume(arguments.task_id)
        elif arguments.command == "release":
            result = operator.release(arguments.task_id)
        elif arguments.command == "expire":
            result = operator.expire(arguments.task_id, arguments.actor)
        elif arguments.command == "review":
            result = operator.review_manifest(arguments.task_id, Path(arguments.output) if arguments.output else None)
        elif arguments.command in APPROVAL_COMMANDS:
            bindings = None
            if arguments.command.startswith("approve-resource"):
                bindings = {"proposal_id": arguments.proposal_id, "candidate_sha256": arguments.candidate_sha256}
            result = operator.approve(arguments.task_id, APPROVAL_COMMANDS[arguments.command], arguments.approved_by, bindings=bindings)
        elif arguments.command == "reject":
            result = operator.reject(arguments.task_id, arguments.actor, arguments.reason)
        elif arguments.command == "cancel":
            result = operator.cancel(arguments.task_id, arguments.actor)
        elif arguments.command == "sync-handoff":
            result = operator.sync_handoff(arguments.task_id, expected_before_hash=arguments.expected_before_hash)
        elif arguments.command == "create-stale-lock-approval":
            result = operator.create_stale_takeover_approval(
                arguments.task_id, arguments.approved_by, Path(arguments.output)
            )
        else:
            raise OperatorError("unknown command")
        _print(result)
        return 0
    except (ContractError, OperatorError, StateStoreError, OSError, ValueError, RuntimeError) as exc:
        print(f"wpi-council blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
