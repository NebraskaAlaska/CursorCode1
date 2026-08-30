from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from flyash_phreeqc_ml.council_operator.contracts import (
    ContractError,
    REQUEST_FIELDS,
    SensitiveContentError,
    TaskRequest,
    canonical_json,
    load_request_file,
    render_request_markdown,
)
from council_operator_helpers import create_code_remote, make_policy, make_request


def test_request_round_trip_and_deterministic_hash(tmp_path):
    remote, _live, head = create_code_remote(tmp_path)
    policy = make_policy(remote)
    request = make_request(policy, head)
    assert TaskRequest.from_dict(json.loads(canonical_json(request.to_dict()))) == request
    assert request.computed_hash() == request.canonical_request_hash
    assert TaskRequest.seal({**request.to_dict(), "canonical_request_hash": ""}).canonical_request_hash == request.canonical_request_hash


def test_request_markdown_is_a_closed_canonical_envelope(tmp_path):
    remote, _live, head = create_code_remote(tmp_path)
    request = make_request(make_policy(remote), head)
    path = tmp_path / "REQUEST.md"
    path.write_text(render_request_markdown(request), encoding="utf-8")
    assert load_request_file(path) == request
    path.write_text(render_request_markdown(request) + "extra", encoding="utf-8")
    with pytest.raises(ContractError):
        load_request_file(path)


def test_unknown_and_missing_request_fields_fail(tmp_path):
    remote, _live, head = create_code_remote(tmp_path)
    value = make_request(make_policy(remote), head).to_dict()
    with pytest.raises(ContractError, match="unknown"):
        TaskRequest.from_dict({**value, "mystery": True})
    value.pop("title")
    with pytest.raises(ContractError, match="missing"):
        TaskRequest.from_dict(value)


@pytest.mark.parametrize("task_id", ["../escape", "/absolute", "UPPER", "a", "bad_id", "-bad"])
def test_unsafe_task_ids_fail(tmp_path, task_id):
    remote, _live, head = create_code_remote(tmp_path)
    request = make_request(make_policy(remote), head).to_dict()
    request["task_id"] = task_id
    request["canonical_request_hash"] = ""
    with pytest.raises(ContractError, match="task ID"):
        TaskRequest.seal(request)


@pytest.mark.parametrize("path", ["../x", "/tmp/x", "C:/x", "docs/../x", "docs\\x", ".git/config"])
def test_path_escape_and_git_paths_fail(tmp_path, path):
    remote, _live, head = create_code_remote(tmp_path)
    request = make_request(make_policy(remote), head).to_dict()
    request["allowed_paths"] = [path]
    request["canonical_request_hash"] = ""
    with pytest.raises(ContractError):
        TaskRequest.seal(request)


@pytest.mark.parametrize(
    "text",
    [
        "sk-" + "proj-abcdefghijklmnopqrstuvwxyz123456",
        "gh" + "p_abcdefghijklmnopqrstuvwxyz123456",
        "password=hunter2",
        "-----BEGIN " + "PRIVATE KEY-----",
    ],
)
def test_secret_bearing_requests_fail_without_echo(tmp_path, text):
    remote, _live, head = create_code_remote(tmp_path)
    value = make_request(make_policy(remote), head).to_dict()
    value["background"] = text
    value["canonical_request_hash"] = ""
    with pytest.raises(SensitiveContentError) as error:
        TaskRequest.seal(value)
    assert text not in str(error.value)


@pytest.mark.parametrize("text", ["Use unpublished measured sample rows", "Read data/raw/private.csv", "Load participant data"])
def test_unapproved_research_data_requests_fail(tmp_path, text):
    remote, _live, head = create_code_remote(tmp_path)
    value = make_request(make_policy(remote), head).to_dict()
    value["goal"] = text
    value["canonical_request_hash"] = ""
    with pytest.raises(SensitiveContentError):
        TaskRequest.seal(value)


def test_public_repo_refuses_private_request_without_private_control(tmp_path):
    remote, _live, head = create_code_remote(tmp_path)
    policy = make_policy(remote)
    request = make_request(policy, head, security_privacy="private")
    with pytest.raises(SensitiveContentError):
        policy.validate_request(request, has_private_control_remote=False)


@pytest.mark.parametrize("intent", ["push main", "force-push this", "bypass tests", "deploy immediately", "change repository visibility"])
def test_control_plane_bypass_intents_fail(tmp_path, intent):
    remote, _live, head = create_code_remote(tmp_path)
    policy = make_policy(remote)
    request = make_request(policy, head, goal=intent)
    with pytest.raises(ContractError, match="prohibited"):
        policy.validate_request(request, has_private_control_remote=True)


def test_forbidden_branch_and_repository_fail(tmp_path):
    remote, _live, head = create_code_remote(tmp_path)
    policy = make_policy(remote)
    with pytest.raises(ContractError):
        policy.validate_request(make_request(policy, head, base_branch="main"), has_private_control_remote=True)
    with pytest.raises(ContractError):
        policy.validate_request(make_request(policy, head, repository="https://example.invalid/other.git"), has_private_control_remote=True)


def test_allowed_glob_cannot_overlap_forbidden_subtree(tmp_path):
    remote, _live, head = create_code_remote(tmp_path)
    policy = make_policy(remote)
    request = make_request(policy, head, allowed_paths=["data/**"])
    with pytest.raises(ContractError, match="intersect"):
        policy.validate_request(request, has_private_control_remote=True)


def test_shell_operators_and_unapproved_commands_fail(tmp_path):
    remote, _live, head = create_code_remote(tmp_path)
    policy = make_policy(remote)
    value = make_request(policy, head).to_dict()
    value["required_test_commands"] = [["python3", "-m", "compileall", ";", "touch", "x"]]
    value["canonical_request_hash"] = ""
    with pytest.raises(ContractError, match="shell"):
        TaskRequest.seal(value)
    request = make_request(policy, head, required_test_commands=[["curl", "https://example.com"]])
    with pytest.raises(ContractError, match="unapproved"):
        policy.validate_request(request, has_private_control_remote=True)


def test_nan_and_forged_hash_fail(tmp_path):
    remote, _live, head = create_code_remote(tmp_path)
    value = make_request(make_policy(remote), head).to_dict()
    value["max_patch_bytes"] = float("nan")
    value["canonical_request_hash"] = ""
    with pytest.raises(ContractError):
        TaskRequest.seal(value)
    value = make_request(make_policy(remote), head).to_dict()
    value["canonical_request_hash"] = "0" * 64
    with pytest.raises(ContractError, match="hash"):
        TaskRequest.from_dict(value)


def test_tracked_json_schemas_are_closed():
    root = Path(__file__).resolve().parents[1]
    for name in ("council-task-request.schema.json", "council-task-state.schema.json", "council-approval.schema.json"):
        schema = json.loads((root / "resources" / "schemas" / name).read_text(encoding="utf-8"))
        assert schema["additionalProperties"] is False


def test_policy_binds_every_authoritative_council_contract():
    root = Path(__file__).resolve().parents[1]
    policy = make_policy(Path("/tmp/synthetic-control.git"))
    assert len(policy.council_required_sha256) == 10
    assert "tools/council_route.py" in policy.council_required_sha256
    assert set(policy.resource_steward_automatic_actions).isdisjoint({"promote", "rollback"})
