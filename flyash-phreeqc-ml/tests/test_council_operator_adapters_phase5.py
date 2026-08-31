from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import subprocess

import pytest

from flyash_phreeqc_ml.council_operator.backends import (
    BackendError,
    COUNCIL_REQUIRED_PROFILES,
    ConfigurableCommandBackend,
    CouncilCompatibilityAdapter,
    HermesBackend,
)
from council_operator_helpers import create_code_remote, make_policy, make_request


ROUTE_SCRIPT = r'''#!/usr/bin/env python3
import argparse, json
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--run', required=True); g=p.add_mutually_exclusive_group(required=True); g.add_argument('--requested-mode'); g.add_argument('--apply-recommendation', action='store_true'); p.add_argument('--estimated-files'); a=p.parse_args()
root=Path(__file__).resolve().parents[1]; out=root/'runs'/a.run/'05_route'; out.mkdir(parents=True, exist_ok=True)
value={'selected_level':'STANDARD','fixture':True}
(out/'ROUTE.json').write_text(json.dumps(value))
print(json.dumps(value))
'''

LAUNCHER_SCRIPT = r'''#!/usr/bin/env python3
import argparse, json, os
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--run', required=True); p.add_argument('--stage', required=True); a=p.parse_args()
root=Path(__file__).resolve().parents[1]; run=root/'runs'/a.run
inv=run/'05_route'/'invocations'; inv.mkdir(parents=True, exist_ok=True); (inv/f'{a.stage}.json').write_text(json.dumps({'stage':a.stage,'fixture':True}))
if a.stage == 'planner':
    out=run/'10_plan'; out.mkdir(); (out/'PLAN_STATUS.json').write_text('{"status":"READY"}'); (out/'ROUTE_RECOMMENDATION.json').write_text('{}')
elif a.stage == 'reviewer':
    out=run/'40_review'; out.mkdir(); (out/'REVIEW_VERDICT.json').write_text('{"verdict":"APPROVE"}'); (out/'REVIEW_REPORT.md').write_text('fixture reviewer report'); (out/'COMPLEXITY_REVIEW.md').write_text('fixture')
print('{}')
'''

GATE_SCRIPT = r'''#!/usr/bin/env python3
import argparse, json
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('mode'); p.add_argument('--run', required=True); a=p.parse_args()
root=Path(__file__).resolve().parents[1]; run=root/'runs'/a.run
if a.mode == 'coder':
    out=run/'20_code'; out.mkdir(); value={'status':'APPLIED','changed_paths':['README.md'],'patch_sha256':'a'*64}
    name='CODER_GATE_STATUS.json'
else:
    out=run/'30_tests'; out.mkdir(); value={'status':'APPLIED_PASS','changed_paths':[],'test':{'exit_code':0}}
    name='TESTER_GATE_STATUS.json'
(out/name).write_text(json.dumps(value)); print(json.dumps(value))
'''


def fixture_council(root: Path, policy, *, planner_blocked=False):
    for directory in ("runs", "sandboxes", ".role-outbox/planner", ".role-outbox/coder", ".role-outbox/tester", ".role-outbox/reviewer"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    contents = {
        "AGENTS.md": "fixture",
        "START_HERE.md": "fixture",
        "prompts/START_PLANNER.md": "fixture",
        "prompts/START_CODER.md": "fixture",
        "prompts/START_TESTER.md": "fixture",
        "prompts/START_REVIEWER.md": "fixture",
        "tools/council_route.py": ROUTE_SCRIPT,
        "tools/council_stage_launcher.py": LAUNCHER_SCRIPT.replace('{"status":"READY"}', '{"status":"BLOCKED"}' if planner_blocked else '{"status":"READY"}'),
        "tools/council_patch_gate.py": GATE_SCRIPT,
        "tools/coder_export_bundle.py": "# fixture exporter",
    }
    hashes = {}
    for relative, text in contents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return replace(policy, council_required_sha256=hashes)


def test_hermes_adapter_invokes_route_stages_and_gates_through_verified_paths(tmp_path):
    remote, live, head = create_code_remote(tmp_path / "repo")
    policy = make_policy(remote)
    council = tmp_path / "runtime" / "AI-Council"
    policy = fixture_council(council, policy)
    request = make_request(policy, head)
    adapter = CouncilCompatibilityAdapter(council, policy)
    backend = HermesBackend(adapter, policy)
    attempt_root = tmp_path / "attempt"
    attempt_root.mkdir()
    result = backend.execute_attempt(request, live, "attempt-00", attempt_root)
    assert result.passed is True
    assert result.route["fixture"] is True
    assert result.reviewer_verdict == "APPROVE"
    assert result.changed_paths == ("README.md",)
    assert result.evidence["raw_role_transcripts_retained"] is False


def test_planner_blocked_stops_before_coder_gate(tmp_path):
    remote, live, head = create_code_remote(tmp_path / "repo")
    policy = make_policy(remote)
    council = tmp_path / "runtime" / "AI-Council"
    policy = fixture_council(council, policy, planner_blocked=True)
    request = make_request(policy, head)
    result = HermesBackend(CouncilCompatibilityAdapter(council, policy), policy).execute_attempt(
        request, live, "attempt-00", tmp_path / "unused"
    )
    assert result.passed is False
    assert result.correction_kind == "planner_blocked"
    assert not (council / "runs" / "wpi-test-task-attempt-00" / "20_code").exists()


def test_prepare_attempt_rejects_nonempty_role_outbox_and_run_reuse(tmp_path):
    remote, live, head = create_code_remote(tmp_path / "repo")
    policy = make_policy(remote)
    council = tmp_path / "runtime" / "AI-Council"
    policy = fixture_council(council, policy)
    adapter = CouncilCompatibilityAdapter(council, policy)
    request = make_request(policy, head)
    (council / ".role-outbox" / "coder" / "unexpected").write_text("x")
    with pytest.raises(BackendError, match="outbox"):
        adapter.prepare_attempt(request, live, "run-one", None)
    (council / ".role-outbox" / "coder" / "unexpected").unlink()
    adapter.prepare_attempt(request, live, "run-one", None)
    with pytest.raises(BackendError, match="immutable"):
        adapter.prepare_attempt(request, live, "run-one", None)


def test_prepared_council_sandbox_has_no_remote_and_binds_exact_commit(tmp_path):
    remote, live, head = create_code_remote(tmp_path / "repo")
    policy = make_policy(remote)
    council = tmp_path / "runtime" / "AI-Council"
    policy = fixture_council(council, policy)
    request = make_request(policy, head)
    run, sandbox = CouncilCompatibilityAdapter(council, policy).prepare_attempt(request, live, "run-two", None)
    assert subprocess.check_output(["git", "-C", sandbox, "remote"], text=True).strip() == ""
    assert subprocess.check_output(["git", "-C", sandbox, "rev-parse", "HEAD"], text=True).strip() == head
    text = (run / "00_REQUEST.md").read_text(encoding="utf-8")
    assert request.canonical_request_hash in text
    assert str(sandbox) in text


def test_command_backend_requires_exact_executable_hash(tmp_path):
    remote, _live, _head = create_code_remote(tmp_path / "repo")
    policy = make_policy(remote)
    council = tmp_path / "runtime" / "AI-Council"
    policy = fixture_council(council, policy)
    executable = tmp_path / "role-cli"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    backend = ConfigurableCommandBackend(
        CouncilCompatibilityAdapter(council, policy),
        policy,
        {role: (str(executable), role) for role in ("planner", "coder", "tester", "reviewer")},
        {role: "0" * 64 for role in ("planner", "coder", "tester", "reviewer")},
    )
    with pytest.raises(BackendError, match="identity"):
        backend._resolved_command("planner")


def test_worker_profile_preflight_requires_every_launcher_selected_profile(tmp_path):
    remote, _live, _head = create_code_remote(tmp_path / "repo")
    policy = make_policy(remote)
    council = tmp_path / "runtime" / "AI-Council"
    policy = fixture_council(council, policy)
    profiles = tmp_path / "profiles"
    for profile in COUNCIL_REQUIRED_PROFILES:
        root = profiles / profile
        root.mkdir(parents=True)
        (root / "config.yaml").write_text("fixture: true\n", encoding="utf-8")
        (root / "SOUL.md").write_text("fixture\n", encoding="utf-8")
        (root / "state.db").write_bytes(b"fixture")
    observed = CouncilCompatibilityAdapter(council, policy).verify_worker_profiles(profiles)
    assert set(observed) == set(COUNCIL_REQUIRED_PROFILES)
    (profiles / "council-reviewer" / "state.db").unlink()
    with pytest.raises(BackendError, match="incomplete"):
        CouncilCompatibilityAdapter(council, policy).verify_worker_profiles(profiles)
