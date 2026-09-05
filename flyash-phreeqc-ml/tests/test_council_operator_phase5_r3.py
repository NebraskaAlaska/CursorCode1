from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import flyash_phreeqc_ml.council_operator.operator as operator_module
from flyash_phreeqc_ml.council_operator.backends import (
    AttemptResult,
    CouncilCompatibilityAdapter,
    estimate_changed_files,
)
from flyash_phreeqc_ml.council_operator.cli import _create_request, build_parser
from flyash_phreeqc_ml.council_operator.contracts import (
    ContractError,
    OperatorConfig,
    ProjectPolicy,
    load_request_file,
)
from flyash_phreeqc_ml.council_operator.operator import CouncilOperator, OperatorError
from flyash_phreeqc_ml.council_operator.runtime import missing_compileall_targets
from flyash_phreeqc_ml.council_operator.sandbox import bounded_process
from council_operator_helpers import (
    create_code_remote,
    make_operator,
    make_policy,
    make_request,
)


PROJECT = Path(__file__).resolve().parents[1]
GIT_ROOT = PROJECT.parent
POLICY_PATH = PROJECT / "config" / "council_operator_policy.toml"
RUNBOOK = PROJECT / "docs" / "hermes_physical_acceptance.md"
SENTINEL_PATH = "flyash-phreeqc-ml/docs/hermes_acceptance_sentinel.md"
TESTER_PATH = "flyash-phreeqc-ml/tests/test_hermes_acceptance_sentinel.py"
R3_TASK_ID = "phase5-r3-hermes-docs-acceptance-01"
R3_TITLE = "Add the sanitized Hermes acceptance sentinel and test"
R3_GOAL = (
    "Create the approved documentation sentinel and its deterministic acceptance "
    "test at the two explicitly allowed repository paths."
)
R3_BACKGROUND = (
    "Sanitized public physical Council-path acceptance using no scientific content."
)
R3_ACCEPTANCE = (
    "Coder changes only the approved documentation path.",
    "Tester changes only the approved test path.",
    "The sentinel contains exactly: This is a documentation-only Council "
    "operator-path acceptance sentinel.",
    "The deterministic test verifies that exact sentinel content.",
    "All corrected immutable trusted tests pass.",
    "The trusted host may separately generate its standard council-results "
    "manifest; no model role writes that namespace.",
)


def _marked_bash(text: str, marker: str) -> str:
    match = re.search(
        rf"<!-- {re.escape(marker)}_BEGIN -->\s*```bash\s*(.*?)\s*```\s*"
        rf"<!-- {re.escape(marker)}_END -->",
        text,
        re.DOTALL,
    )
    assert match
    return match.group(1)


def _computer_sections(text: str, owner: str) -> str:
    sections = re.findall(
        rf"^## {re.escape(owner)} COMPUTER[^\n]*\n(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert sections
    return "\n".join(sections)


def _controller_commands(text: str) -> list[tuple[str, ...]]:
    commands: list[tuple[str, ...]] = []
    for block in re.findall(r"```bash\s*(.*?)\s*```", text, re.DOTALL):
        for line in block.replace("\\\n", " ").splitlines():
            stripped = line.strip()
            if stripped.startswith(("council_personal ", "wpi-council ")):
                commands.append(tuple(shlex.split(stripped)))
    return commands


def _assert_r3_r1_runbook_contract(text: str) -> None:
    preflight = _marked_bash(text, "R3_PERSONAL_PREFLIGHT")
    assert (
        'export WPI_PERSONAL_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/'
        'wpi-virtual-lab/council-personal.toml"'
    ) in preflight
    assert 'export WPI_PERSONAL_PYTHON="$WPI_PROJECT/.venv/bin/python"' in preflight
    assert 'test -f "$WPI_PERSONAL_CONFIG"' in preflight
    assert "test \"$WPI_PERSONAL_CONFIG_MODE\" = '600'" in preflight
    assert 'test -x "$WPI_PERSONAL_PYTHON"' in preflight
    assert "sys.version_info[:2] == (3, 12)" in preflight
    assert 'config["operator"]["repository_path"]' in preflight
    assert 'expected = Path(sys.argv[2]).resolve()' in preflight
    assert '"$WPI_PERSONAL_CONFIG" "$WPI_CHECKOUT"' in preflight

    wrapper = re.search(r"council_personal\(\)\s*\{(.*?)\n\}", preflight, re.DOTALL)
    assert wrapper
    wrapper_text = wrapper.group(1)
    assert wrapper_text.index('cd "$WPI_PROJECT"') < wrapper_text.index(
        '"$WPI_PERSONAL_PYTHON"'
    )
    assert "-m flyash_phreeqc_ml.council_operator.cli" in wrapper_text
    assert '--config "$WPI_PERSONAL_CONFIG"' in wrapper_text
    assert wrapper_text.index("-m flyash_phreeqc_ml.council_operator.cli") < (
        wrapper_text.index('--config "$WPI_PERSONAL_CONFIG"')
    ) < wrapper_text.index('"$@"')

    personal = _computer_sections(text, "PERSONAL")
    personal_commands = _controller_commands(personal)
    assert all(command[0] != "wpi-council" for command in personal_commands)
    personal_pairs = {(command[0], command[1]) for command in personal_commands}
    assert {
        ("council_personal", "doctor"),
        ("council_personal", "create-request"),
        ("council_personal", "submit"),
        ("council_personal", "status"),
        ("council_personal", "watch"),
        ("council_personal", "review"),
        ("council_personal", "approve-task-branch"),
        ("council_personal", "reject"),
    }.issubset(personal_pairs)
    assert personal.index("council_personal doctor") < personal.index(
        "council_personal create-request"
    )
    assert f"export WPI_TASK_ID='{R3_TASK_ID}'" in personal
    assert "worker_ready=false" in personal
    assert "live Council root" in text
    assert "Hermes executable" in text

    hermes = _computer_sections(text, "HERMES")
    hermes_commands = _controller_commands(hermes)
    assert any(command[:3] == ("wpi-council", "doctor", "--worker") for command in hermes_commands)
    assert any(command[:2] == ("wpi-council", "release") for command in hermes_commands)
    assert "council_personal" not in hermes
    assert "council-personal.toml" not in hermes
    assert "WPI_PERSONAL_CONFIG" not in hermes


def _personal_guard_script() -> str:
    preflight = _marked_bash(RUNBOOK.read_text(encoding="utf-8"), "R3_PERSONAL_PREFLIGHT")
    return preflight[
        preflight.index('test -f "$WPI_PERSONAL_CONFIG"') :
        preflight.index("\ncouncil_personal()")
    ]


def _run_personal_guard(
    tmp_path: Path,
    *,
    config_present: bool = True,
    config_mode: int = 0o600,
    python_present: bool = True,
    python_312: bool = True,
    repository_is_project: bool = False,
) -> subprocess.CompletedProcess[str]:
    checkout = tmp_path / "VirtualLAB-Codex"
    project = checkout / "flyash-phreeqc-ml"
    python = project / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    if python_present:
        if python_312:
            python.symlink_to(Path(sys.executable).resolve())
        else:
            python.write_text("#!/bin/sh\nprintf 'wrong\\n'\n", encoding="utf-8")
            python.chmod(0o755)

    config = tmp_path / "config" / "wpi-virtual-lab" / "council-personal.toml"
    if config_present:
        config.parent.mkdir(parents=True)
        repository = project if repository_is_project else checkout
        config.write_text(
            f'[operator]\nrepository_path = "{repository}"\n', encoding="utf-8"
        )
        config.chmod(config_mode)

    environment = {
        **os.environ,
        "WPI_CHECKOUT": str(checkout),
        "WPI_PROJECT": str(project),
        "WPI_PERSONAL_CONFIG": str(config),
        "WPI_PERSONAL_PYTHON": str(python),
    }
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", _personal_guard_script()],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )


def _real_policy(remote: Path, head_branch: str = "base") -> ProjectPolicy:
    return replace(
        ProjectPolicy.load(POLICY_PATH),
        allowed_repository=str(remote),
        allowed_base_branches=(head_branch,),
        forbidden_branches=("main", "master"),
    )


def _attempt(workspace: Path, changed_paths: tuple[str, ...]) -> AttemptResult:
    return AttemptResult(
        attempt_id="attempt-00",
        passed=True,
        correction_kind=None,
        correction_detail="",
        workspace=workspace,
        route={"selected_level": "STANDARD"},
        changed_paths=changed_paths,
        patch_hash="1" * 64,
        test_evidence_hash="2" * 64,
        reviewer_report_hash="3" * 64,
        reviewer_verdict="APPROVE",
        evidence={"invocation_evidence_sha256": {}},
    )


def _remote_sha(remote: Path, branch: str) -> str | None:
    output = subprocess.check_output(
        ["git", "ls-remote", "--heads", str(remote), f"refs/heads/{branch}"],
        text=True,
    ).strip()
    return output.split("\t", 1)[0] if output else None


def _commit_fixture(live: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(live), "add", "--all"], check=True)
    subprocess.run(
        ["git", "-C", str(live), "commit", "-m", message],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(live), "push", "origin", "base"],
        check=True,
        capture_output=True,
    )
    return subprocess.check_output(
        ["git", "-C", str(live), "rev-parse", "HEAD"], text=True
    ).strip()


def _minimal_project_tree(root: Path) -> None:
    for relative in (
        "flyash-phreeqc-ml/app.py",
        "flyash-phreeqc-ml/app_ui.py",
        "flyash-phreeqc-ml/flyash_phreeqc_ml/module.py",
        "flyash-phreeqc-ml/scripts/tool.py",
        "flyash-phreeqc-ml/ui/page.py",
        "flyash-phreeqc-ml/tests/test_fixture.py",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VALUE = 1\n", encoding="utf-8")


def test_repository_and_application_roots_are_explicit_and_distinct(tmp_path):
    policy = ProjectPolicy.load(POLICY_PATH)
    repository = tmp_path / "VirtualLAB-Codex"
    _minimal_project_tree(repository)
    subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
    application = repository / policy.application_subtree
    assert (repository / ".git").is_dir()
    assert application.parent == repository
    assert policy.application_subtree == "flyash-phreeqc-ml"
    if (GIT_ROOT / ".git").is_dir():
        assert GIT_ROOT / policy.application_subtree == PROJECT


def test_loaded_package_derives_same_subtree_from_configured_git_root(tmp_path, monkeypatch):
    policy = ProjectPolicy.load(POLICY_PATH)
    repository = tmp_path / "VirtualLAB-Codex"
    _minimal_project_tree(repository)
    package_file = (
        repository
        / policy.application_subtree
        / "flyash_phreeqc_ml"
        / "council_operator"
        / "operator.py"
    )
    package_file.parent.mkdir(parents=True, exist_ok=True)
    package_file.write_text("# layout fixture\n", encoding="utf-8")
    monkeypatch.setattr(operator_module, "__file__", str(package_file))
    config = OperatorConfig(
        repository_path=repository,
        code_remote=policy.allowed_repository,
        control_remote=policy.allowed_repository,
        worker_id="r3-layout-test",
        sandbox_root=tmp_path / "sandboxes",
        state_cache_root=tmp_path / "state",
        council_root=None,
        hermes_executable=None,
        obsidian_vault=None,
    )
    operator = CouncilOperator(config, policy)
    assert operator.application_subtree.as_posix() == policy.application_subtree
    with pytest.raises(OperatorError, match="application subtree"):
        CouncilOperator(
            replace(config, repository_path=repository / policy.application_subtree),
            policy,
        )


def test_corrected_default_compile_targets_exist_and_are_git_root_relative():
    policy = ProjectPolicy.load(POLICY_PATH)
    command = policy.default_test_commands[0]
    assert command[:4] == ("python3", "-m", "compileall", "-q")
    assert command[4:] == (
        "flyash-phreeqc-ml/app.py",
        "flyash-phreeqc-ml/app_ui.py",
        "flyash-phreeqc-ml/flyash_phreeqc_ml",
        "flyash-phreeqc-ml/scripts",
        "flyash-phreeqc-ml/ui",
        "flyash-phreeqc-ml/tests",
    )
    for target in command[4:]:
        parts = Path(target).parts
        assert parts[0] == policy.application_subtree
        assert (PROJECT.joinpath(*parts[1:])).exists()


def test_corrected_compile_command_visits_nested_project_files(tmp_path):
    _minimal_project_tree(tmp_path)
    command = ProjectPolicy.load(POLICY_PATH).default_test_commands[0]
    result = bounded_process(
        (sys.executable, *command[1:]),
        cwd=tmp_path,
        timeout_seconds=60,
        max_output_bytes=65536,
    )
    assert result["exit_code"] == 0
    assert len(tuple((tmp_path / "flyash-phreeqc-ml").rglob("*.pyc"))) == 6


def test_nested_syntax_error_makes_corrected_compile_command_nonzero(tmp_path):
    _minimal_project_tree(tmp_path)
    broken = tmp_path / "flyash-phreeqc-ml" / "tests" / "test_fixture.py"
    broken.write_text("def broken(:\n", encoding="utf-8")
    command = ProjectPolicy.load(POLICY_PATH).default_test_commands[0]
    result = bounded_process(
        (sys.executable, *command[1:]),
        cwd=tmp_path,
        timeout_seconds=60,
        max_output_bytes=65536,
    )
    assert result["exit_code"] != 0


def test_missing_compile_target_cannot_supply_false_green_evidence(tmp_path):
    command = ("python3", "-m", "compileall", "-q", "flyash-phreeqc-ml/missing")
    raw = subprocess.run(
        [sys.executable, *command[1:]],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    assert raw.returncode == 0
    assert b"Can't list" in raw.stdout + raw.stderr
    assert missing_compileall_targets(command, tmp_path) == ("flyash-phreeqc-ml/missing",)


def test_operator_records_compile_target_preflight_and_requested_effective_argv(tmp_path):
    remote, live, head = create_code_remote(tmp_path / "repo")
    policy = replace(
        make_policy(remote),
        allowed_test_command_prefixes=(("python3", "-m", "compileall"),),
    )
    operator = make_operator(tmp_path, remote, live, policy)
    command = ["python3", "-m", "compileall", "-q", "flyash-phreeqc-ml/missing"]
    request = make_request(policy, head, required_test_commands=[command])
    checked = operator._run_required_tests(
        request, _attempt(live, ()), time.monotonic()
    )
    record = checked.evidence["trusted_required_tests"][0]
    assert checked.passed is False
    assert record["exit_code"] == 2
    assert record["requested_arguments"] == command
    assert record["effective_arguments"][0] == operator.python_runtime.executable
    assert record["preflight_error"] == "compileall target contract is missing or unsafe"


def test_corrected_pytest_command_discovers_only_nested_test_tree(tmp_path):
    test_file = tmp_path / TESTER_PATH
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_nested_discovery():\n    assert True\n", encoding="utf-8")
    command = ProjectPolicy.load(POLICY_PATH).default_test_commands[1]
    assert command == (
        "python3", "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "flyash-phreeqc-ml/tests",
    )
    result = bounded_process(
        (sys.executable, *command[1:]),
        cwd=tmp_path,
        timeout_seconds=60,
        max_output_bytes=65536,
    )
    assert result["exit_code"] == 0
    assert "1 passed" in result["stdout"]


@pytest.mark.parametrize(
    "path",
    [
        "flyash-phreeqc-ml/data/raw/**",
        "flyash-phreeqc-ml/experiments/run-1/data/**",
        "flyash-phreeqc-ml/experiments/run-1/outputs/**",
        "flyash-phreeqc-ml/outputs/**",
        "flyash-phreeqc-ml/models/**",
        "flyash-phreeqc-ml/databases/**",
        "flyash-phreeqc-ml/resources/installed/**",
        "flyash-phreeqc-ml/resources/downloads/**",
        "flyash-phreeqc-ml/resources/candidates/**",
        "flyash-phreeqc-ml/resources/active/**",
        "flyash-phreeqc-ml/config/council_operator_policy.toml",
        "council-results/role-authored.json",
    ],
)
def test_nested_forbidden_paths_cannot_be_allowed(tmp_path, path):
    remote, _live, head = create_code_remote(tmp_path / "repo")
    policy = _real_policy(remote)
    with pytest.raises(ContractError, match="intersect"):
        request = make_request(
            policy, head, allowed_paths=[path], security_privacy="private"
        )
        policy.validate_request(request, has_private_control_remote=True)


@pytest.mark.parametrize(
    "path",
    ["**", "flyash-phreeqc-ml/**", "flyash-phreeqc-ml/resources/**"],
)
def test_broad_equivalent_allowlists_cannot_encompass_nested_forbidden_paths(tmp_path, path):
    remote, _live, head = create_code_remote(tmp_path / "repo")
    policy = _real_policy(remote)
    request = make_request(policy, head, allowed_paths=[path])
    with pytest.raises(ContractError, match="intersect"):
        policy.validate_request(request, has_private_control_remote=True)


@pytest.mark.parametrize(
    "path",
    [
        "flyash-phreeqc-ml/flyash_phreeqc_ml/**",
        "flyash-phreeqc-ml/tests/**",
        SENTINEL_PATH,
        TESTER_PATH,
    ],
)
def test_normal_nested_source_test_and_documentation_paths_remain_usable(tmp_path, path):
    remote, _live, head = create_code_remote(tmp_path / "repo")
    policy = _real_policy(remote)
    request = make_request(policy, head, allowed_paths=[path])
    policy.validate_request(request, has_private_control_remote=True)


def test_router_estimator_counts_unique_exact_paths_and_bounds_result(tmp_path):
    remote, _live, head = create_code_remote(tmp_path / "repo")
    policy = make_policy(remote)
    one = make_request(policy, head, allowed_paths=[SENTINEL_PATH], max_changed_files=8)
    two = make_request(
        policy, head, allowed_paths=[SENTINEL_PATH, TESTER_PATH], max_changed_files=8
    )
    duplicate = make_request(
        policy,
        head,
        allowed_paths=[SENTINEL_PATH, SENTINEL_PATH],
        max_changed_files=8,
    )
    bounded = make_request(
        policy, head, allowed_paths=[SENTINEL_PATH, TESTER_PATH], max_changed_files=1
    )
    assert estimate_changed_files(one) == 1
    assert estimate_changed_files(two) == 2
    assert estimate_changed_files(duplicate) == 1
    assert estimate_changed_files(bounded) == 1


def test_router_estimator_uses_hard_cap_for_glob(tmp_path):
    remote, _live, head = create_code_remote(tmp_path / "repo")
    policy = make_policy(remote)
    request = make_request(
        policy,
        head,
        allowed_paths=["docs/**"],
        max_changed_files=7,
    )
    assert estimate_changed_files(request) == 7


def test_hermes_backend_passes_estimate_to_router_and_retains_evidence(
    tmp_path, monkeypatch
):
    remote, live, head = create_code_remote(tmp_path / "repo")
    policy = make_policy(remote)
    request = make_request(
        policy,
        head,
        allowed_paths=[SENTINEL_PATH, TESTER_PATH],
        max_changed_files=8,
    )
    adapter = CouncilCompatibilityAdapter(tmp_path / "council", policy)
    observed: list[int] = []
    monkeypatch.setattr(
        adapter,
        "prepare_attempt",
        lambda *_args: (tmp_path / "run", live),
    )
    monkeypatch.setattr(
        adapter,
        "route",
        lambda _run_id, estimate: observed.append(estimate) or {"selected_level": "STANDARD"},
    )
    (tmp_path / "run" / "10_plan").mkdir(parents=True)
    (tmp_path / "run" / "10_plan" / "PLAN_STATUS.json").write_text(
        '{"status":"BLOCKED"}', encoding="utf-8"
    )
    from flyash_phreeqc_ml.council_operator.backends import HermesBackend

    monkeypatch.setattr(HermesBackend, "invoke_stage", lambda *_args: None)
    result = HermesBackend(adapter, policy).execute_attempt(
        request, live, "attempt-00", tmp_path / "attempt"
    )
    assert observed == [2]
    assert result.evidence["router_estimated_files"] == 2


def test_rendered_request_states_git_root_role_and_trusted_host_contracts(tmp_path):
    remote, _live, head = create_code_remote(tmp_path / "repo")
    policy = make_policy(remote)
    request = make_request(
        policy,
        head,
        allowed_paths=[SENTINEL_PATH, TESTER_PATH],
        required_test_commands=[
            ["python3", "-m", "compileall", "-q", "flyash-phreeqc-ml/tests"],
            ["python3", "-m", "pytest", "-q", "flyash-phreeqc-ml/tests"],
        ],
    )
    rendered = CouncilCompatibilityAdapter(tmp_path / "council", policy).render_council_request(
        request, "run-one", tmp_path / "sandbox", None
    )
    for required in (
        "This path is the Git repository root",
        "`flyash-phreeqc-ml/` relative to that Git root",
        "Git-root-relative allowed paths",
        "Git-root-relative forbidden paths",
        "Every role patch is interpreted against the Git-root workspace",
        "Coder and Tester changes must both remain inside",
        f"`council-results/{request.task_id}.json` is generated by the trusted host",
        "must not write `council-results/**`",
        "Trusted test cwd:** Git repository root",
        "Immutable test 1",
        "Immutable test 2",
    ):
        assert required in rendered
    assert all(shlex.join(command) in rendered for command in request.required_test_commands)


def _runbook_request(tmp_path: Path, remote: Path, head: str, *, text: str | None = None):
    text = text if text is not None else RUNBOOK.read_text(encoding="utf-8")
    match = re.search(
        r"<!-- R3_CREATE_REQUEST_BEGIN -->\s*```bash\s*(.*?)\s*```\s*<!-- R3_CREATE_REQUEST_END -->",
        text,
        re.DOTALL,
    )
    assert match
    command = match.group(1).replace("\\\n", " ").replace("$WPI_TASK_ID", R3_TASK_ID)
    words = shlex.split(command)
    assert words[:2] == ["council_personal", "create-request"]
    arguments = build_parser().parse_args(words[1:])
    arguments.output = str(tmp_path / f"{R3_TASK_ID}.md")
    policy = _real_policy(remote)
    operator = SimpleNamespace(
        policy=policy,
        config=SimpleNamespace(has_private_control_remote=False),
        sandboxes=SimpleNamespace(
            verify_live_checkout=lambda: {"branch": "base", "head": head}
        ),
    )
    _create_request(operator, arguments)
    return load_request_file(Path(arguments.output)), match.group(1)


def test_exact_r3_runbook_request_is_valid_and_has_two_role_paths(tmp_path):
    remote, _live, head = create_code_remote(tmp_path / "repo")
    request, command_text = _runbook_request(tmp_path, remote, head)
    assert request.task_id == R3_TASK_ID
    assert request.title == R3_TITLE
    assert request.goal == R3_GOAL
    assert request.background == R3_BACKGROUND
    assert request.allowed_paths == (SENTINEL_PATH, TESTER_PATH)
    assert request.acceptance_criteria == R3_ACCEPTANCE
    assert request.max_changed_files == 2
    assert request.required_test_commands == ProjectPolicy.load(POLICY_PATH).default_test_commands
    assert request.relevant_documentation == (
        "flyash-phreeqc-ml/docs/council_operator.md",
    )
    assert request.scientific_risk == "moderate"
    assert request.security_privacy == "sanitized"
    assert request.requested_backend == "hermes"
    assert request.requester_identity == "human:maintainer"
    searchable = " ".join(
        (request.goal, request.background, *request.acceptance_criteria)
    ).lower()
    assert "deploy" not in searchable
    assert "deploy" not in command_text.lower()
    assert "council-results" in searchable
    assert (
        "The sentinel contains exactly: This is a documentation-only Council "
        "operator-path acceptance sentinel."
    ) in request.acceptance_criteria


def test_r3_r1_runbook_uses_explicit_personal_controller_and_separate_hermes_launcher():
    _assert_r3_r1_runbook_contract(RUNBOOK.read_text(encoding="utf-8"))


def test_personal_preflight_accepts_mode_0600_python_312_and_git_root_repository(tmp_path):
    result = _run_personal_guard(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("fixture", "expected_fragment"),
    [
        ({"config_present": False}, ""),
        ({"config_mode": 0o644}, ""),
        ({"python_present": False}, ""),
        ({"python_312": False}, ""),
        (
            {"repository_is_project": True},
            "Personal repository_path must resolve to WPI_CHECKOUT",
        ),
    ],
    ids=(
        "missing-config",
        "wrong-config-permissions",
        "missing-project-python",
        "wrong-python-version",
        "application-subtree-is-not-git-root",
    ),
)
def test_personal_preflight_failures_stop_before_controller(
    tmp_path, fixture, expected_fragment
):
    result = _run_personal_guard(tmp_path, **fixture)
    assert result.returncode != 0
    assert expected_fragment in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("original", "mutated"),
    [
        ("council_personal doctor", "wpi-council doctor"),
        ("council_personal create-request", "wpi-council create-request"),
    ],
    ids=("bare-personal-doctor", "bare-personal-create-request"),
)
def test_bare_personal_launcher_regressions_fail_the_runbook_contract(original, mutated):
    text = RUNBOOK.read_text(encoding="utf-8")
    with pytest.raises(AssertionError):
        _assert_r3_r1_runbook_contract(text.replace(original, mutated, 1))


def test_hermes_worker_cannot_be_rewritten_to_use_the_personal_controller():
    text = RUNBOOK.read_text(encoding="utf-8")
    mutated = text.replace(
        "wpi-council doctor --worker",
        'council_personal --config "$WPI_PERSONAL_CONFIG" doctor --worker',
        1,
    )
    with pytest.raises(AssertionError):
        _assert_r3_r1_runbook_contract(mutated)


@pytest.mark.parametrize(
    ("original", "mutated"),
    [
        ("--max-changed-files 2", "--max-changed-files 3"),
        (R3_TASK_ID, "phase5-r3-hermes-docs-acceptance-02"),
    ],
    ids=("request-payload", "task-id"),
)
def test_r3_request_or_task_id_regressions_fail_validation(
    tmp_path, original, mutated
):
    remote, _live, head = create_code_remote(tmp_path / "repo")
    text = RUNBOOK.read_text(encoding="utf-8").replace(original, mutated)
    if original == R3_TASK_ID:
        with pytest.raises(AssertionError):
            _assert_r3_r1_runbook_contract(text)
    else:
        request, _command_text = _runbook_request(tmp_path, remote, head, text=text)
        assert request.max_changed_files != 2


def test_nested_release_scanner_is_invoked_and_success_permits_push(tmp_path, monkeypatch):
    remote, live, head = create_code_remote(tmp_path / "repo")
    policy = make_policy(remote)
    operator = make_operator(tmp_path, remote, live, policy)
    request = make_request(policy, head, task_id="scan-success")
    (live / "README.md").write_text("# changed\n", encoding="utf-8")
    observed = {}

    def pass_scan(arguments, **kwargs):
        observed["arguments"] = list(arguments)
        observed["cwd"] = kwargs["cwd"]
        return {
            "arguments": list(arguments),
            "exit_code": 0,
            "timed_out": False,
            "truncated": False,
            "stdout_sha256": "4" * 64,
            "stderr_sha256": "5" * 64,
        }

    monkeypatch.setattr(
        "flyash_phreeqc_ml.council_operator.operator.bounded_process", pass_scan
    )
    result = operator._commit_and_push(
        request, live, _attempt(live, ("README.md",)), "fake"
    )
    assert observed["cwd"] == live
    assert observed["arguments"] == [
        operator.python_runtime.executable,
        str(live / "flyash-phreeqc-ml" / "scripts" / "release_scan.py"),
        "--working-tree",
    ]
    assert _remote_sha(remote, result["task_branch"]) == result["final_task_commit"]


def test_release_scanner_refusal_blocks_commit_and_push(tmp_path, monkeypatch):
    remote, live, head = create_code_remote(tmp_path / "repo")
    policy = make_policy(remote)
    operator = make_operator(tmp_path, remote, live, policy)
    request = make_request(policy, head, task_id="scan-refusal")
    (live / "README.md").write_text("# changed\n", encoding="utf-8")
    monkeypatch.setattr(
        "flyash_phreeqc_ml.council_operator.operator.bounded_process",
        lambda arguments, **_kwargs: {
            "arguments": list(arguments),
            "exit_code": 1,
            "timed_out": False,
            "truncated": False,
            "stdout_sha256": "4" * 64,
            "stderr_sha256": "5" * 64,
        },
    )
    with pytest.raises(OperatorError, match="scan rejected"):
        operator._commit_and_push(
            request, live, _attempt(live, ("README.md",)), "fake"
        )
    assert subprocess.check_output(
        ["git", "-C", str(live), "rev-parse", "HEAD"], text=True
    ).strip() == head
    assert _remote_sha(remote, "council/wpi/scan-refusal") is None


def test_missing_release_scanner_fails_closed_without_push(tmp_path):
    remote, live, _head = create_code_remote(tmp_path / "repo")
    (live / "flyash-phreeqc-ml" / "scripts" / "release_scan.py").unlink()
    head = _commit_fixture(live, "remove scanner")
    policy = make_policy(remote)
    operator = make_operator(tmp_path, remote, live, policy)
    request = make_request(policy, head, task_id="scan-missing")
    (live / "README.md").write_text("# changed\n", encoding="utf-8")
    with pytest.raises(OperatorError, match="scanner is missing or unsafe"):
        operator._commit_and_push(
            request, live, _attempt(live, ("README.md",)), "fake"
        )
    assert _remote_sha(remote, "council/wpi/scan-missing") is None


def test_symlink_release_scanner_fails_closed_without_push(tmp_path):
    remote, live, _head = create_code_remote(tmp_path / "repo")
    scanner = live / "flyash-phreeqc-ml" / "scripts" / "release_scan.py"
    target = scanner.with_name("scanner_target.txt")
    scanner.unlink()
    target.write_text("not executable scanner content\n", encoding="utf-8")
    scanner.symlink_to(target.name)
    head = _commit_fixture(live, "symlink scanner")
    policy = make_policy(remote)
    operator = make_operator(tmp_path, remote, live, policy)
    request = make_request(policy, head, task_id="scan-symlink")
    (live / "README.md").write_text("# changed\n", encoding="utf-8")
    with pytest.raises(OperatorError, match="scanner is missing or unsafe"):
        operator._commit_and_push(
            request, live, _attempt(live, ("README.md",)), "fake"
        )
    assert _remote_sha(remote, "council/wpi/scan-symlink") is None


def test_final_workspace_accepts_two_role_paths_plus_trusted_manifest(tmp_path):
    remote, live, head = create_code_remote(tmp_path / "repo")
    policy = make_policy(remote)
    operator = make_operator(tmp_path, remote, live, policy)
    request = make_request(
        policy,
        head,
        task_id="r3-final-workspace",
        allowed_paths=[SENTINEL_PATH, TESTER_PATH],
        max_changed_files=2,
    )
    sentinel = live / SENTINEL_PATH
    test_path = live / TESTER_PATH
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    test_path.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(
        "This is a documentation-only Council operator-path acceptance sentinel.\n",
        encoding="utf-8",
    )
    test_path.write_text(
        "def test_sentinel_contract():\n    assert True\n", encoding="utf-8"
    )
    summary = operator._commit_and_push(
        request, live, _attempt(live, (SENTINEL_PATH, TESTER_PATH)), "fake"
    )
    tree = subprocess.check_output(
        [
            "git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only",
            summary["final_task_commit"],
        ],
        text=True,
    ).splitlines()
    assert SENTINEL_PATH in tree
    assert TESTER_PATH in tree
    assert f"council-results/{request.task_id}.json" in tree


def test_final_workspace_rejects_any_third_role_authored_path(tmp_path):
    remote, live, head = create_code_remote(tmp_path / "repo")
    policy = make_policy(remote)
    operator = make_operator(tmp_path, remote, live, policy)
    request = make_request(
        policy,
        head,
        task_id="r3-third-path",
        allowed_paths=[SENTINEL_PATH, TESTER_PATH],
        max_changed_files=2,
    )
    for relative in (SENTINEL_PATH, TESTER_PATH, "flyash-phreeqc-ml/docs/third.md"):
        path = live / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("sanitized fixture\n", encoding="utf-8")
    with pytest.raises(OperatorError, match="outside the immutable request"):
        operator._commit_and_push(
            request,
            live,
            _attempt(live, (SENTINEL_PATH, TESTER_PATH)),
            "fake",
        )
    assert _remote_sha(remote, "council/wpi/r3-third-path") is None


def test_max_changed_files_remains_final_hard_cap(tmp_path):
    remote, live, head = create_code_remote(tmp_path / "repo")
    policy = make_policy(remote)
    operator = make_operator(tmp_path, remote, live, policy)
    request = make_request(
        policy,
        head,
        task_id="r3-hard-cap",
        allowed_paths=["docs/**"],
        max_changed_files=2,
    )
    for name in ("one.md", "two.md", "three.md"):
        (live / "docs" / name).write_text("sanitized fixture\n", encoding="utf-8")
    with pytest.raises(OperatorError, match="changed-file limit"):
        operator._commit_and_push(
            request,
            live,
            _attempt(live, ("docs/one.md", "docs/two.md", "docs/three.md")),
            "fake",
        )
    assert _remote_sha(remote, "council/wpi/r3-hard-cap") is None


def test_phase5_ci_runs_r3_functional_contract_before_markers():
    workflow_path = Path(
        os.environ.get(
            "WPI_PHASE5_CI_WORKFLOW",
            GIT_ROOT / ".github" / "workflows" / "ci.yml",
        )
    )
    workflow = workflow_path.read_text(encoding="utf-8")
    test_position = workflow.index("tests/test_council_operator_phase5_r3.py")
    for marker in (
        "GIT_ROOT_PATH_CONTRACT_PASS=True",
        "NONVACUOUS_DEFAULT_TESTS_PASS=True",
        "NESTED_FORBIDDEN_PATHS_PASS=True",
        "NESTED_RELEASE_SCAN_REQUIRED=True",
        "ROUTER_ESTIMATE_NOT_MAX_CAP=True",
        "PLANNER_RENDER_ROOT_CONTRACT_PASS=True",
        "R3_ACCEPTANCE_REQUEST_VALID=True",
        "TESTER_HAS_SAFE_NONEMPTY_PATH=True",
    ):
        assert workflow.index(marker) > test_position
