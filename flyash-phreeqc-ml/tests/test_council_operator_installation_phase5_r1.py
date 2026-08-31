from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
COUNCIL_FILES = (
    "AGENTS.md",
    "START_HERE.md",
    "prompts/START_PLANNER.md",
    "prompts/START_CODER.md",
    "prompts/START_TESTER.md",
    "prompts/START_REVIEWER.md",
    "tools/council_route.py",
    "tools/council_stage_launcher.py",
    "tools/council_patch_gate.py",
    "tools/coder_export_bundle.py",
)
PROFILES = (
    "council-planner-routine",
    "council-planner-standard",
    "council-planner-complex",
    "council-planner-critical",
    "council-coder-routine",
    "council-coder-standard",
    "council-coder-complex",
    "council-coder-critical",
    "council-tester",
    "council-reviewer",
)


def _copy_project(target: Path) -> Path:
    project = target / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "config").mkdir()
    (project / "flyash_phreeqc_ml").mkdir()
    shutil.copy2(ROOT / "flyash_phreeqc_ml" / "__init__.py", project / "flyash_phreeqc_ml" / "__init__.py")
    shutil.copytree(
        ROOT / "flyash_phreeqc_ml" / "council_operator",
        project / "flyash_phreeqc_ml" / "council_operator",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for name in (
        "install-council-operator.sh",
        "resolve-council-python.sh",
        "check-council-operator.sh",
        "run-hermes-task.sh",
        "validate_dependency_lock.py",
    ):
        shutil.copy2(ROOT / "scripts" / name, project / "scripts" / name)
    for name in ("council_operator.example.toml", "council_operator_policy.toml"):
        shutil.copy2(ROOT / "config" / name, project / "config" / name)
    for name in (
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "constraints-py312.txt",
        "README.md",
    ):
        shutil.copy2(ROOT / name, project / name)
    return project


def _make_pinned_venv_at(project: Path, venv: Path) -> Path:
    """Make a disposable venv backed by this checkout's already pinned wheels.

    The test never contacts an index.  A .pth file exposes only the dependency
    set already validated in the repository's Python 3.12 environment; the
    editable project installation itself is written into this disposable venv.
    """

    python = venv / "bin" / "python"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)],
        check=True,
        capture_output=True,
        text=True,
    )
    parent_site = subprocess.check_output(
        [sys.executable, "-c", "import site; print(site.getsitepackages()[0])"],
        text=True,
    ).strip()
    child_site = subprocess.check_output(
        [str(python), "-c", "import site; print(site.getsitepackages()[0])"],
        text=True,
    ).strip()
    (Path(child_site) / "approved-pinned-test-environment.pth").write_text(
        parent_site + "\n" + str(project) + "\n", encoding="utf-8"
    )
    return python


def _make_temporary_pinned_venv(project: Path) -> Path:
    return _make_pinned_venv_at(project, project / ".venv")


def _write_runtime_record(record: Path, python: Path, project: Path) -> None:
    version = subprocess.check_output(
        [str(python), "-c", "import platform; print(platform.python_version())"],
        text=True,
    ).strip()
    digest = hashlib.sha256(python.read_bytes()).hexdigest()
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        "\n".join(
            (
                "wpi-council-python-runtime/v1",
                str(python),
                version,
                digest,
                str(project),
                "",
            )
        ),
        encoding="utf-8",
    )
    record.chmod(0o600)


def _existing_dedicated_runtime_fixture(
    tmp_path: Path,
    *,
    create_project_venv: bool = True,
) -> tuple[Path, Path, Path, dict[str, str], Path]:
    project = _copy_project(tmp_path)
    council = _make_council(project, tmp_path)
    home = tmp_path / "home"
    _make_profiles(home)
    environment, _marker = _environment(tmp_path, council)
    install_root = home / ".local" / "share" / "wpi-council"
    dedicated_python = _make_pinned_venv_at(project, install_root / "venv")
    runtime_record = install_root / "operator-python.runtime"
    _write_runtime_record(runtime_record, dedicated_python, project)

    if create_project_venv:
        # This checkout environment appeared after the dedicated installation
        # was recorded. Resolver priority must not silently replace it.
        project_python = _make_temporary_pinned_venv(project)
        assert project_python != dedicated_python
    return project, home, dedicated_python, environment, runtime_record


def _make_council(project: Path, target: Path) -> Path:
    council = target / "live-council-fixture"
    hashes: dict[str, str] = {}
    for relative in COUNCIL_FILES:
        path = council / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"synthetic contract: {relative}\n", encoding="utf-8")
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    for directory in ("runs", "sandboxes", ".role-outbox/planner", ".role-outbox/coder", ".role-outbox/tester", ".role-outbox/reviewer"):
        (council / directory).mkdir(parents=True, exist_ok=True)

    policy_path = project / "config" / "council_operator_policy.toml"
    policy = policy_path.read_text(encoding="utf-8")
    for relative, digest in hashes.items():
        pattern = rf'("{re.escape(relative)}"\s*=\s*")[0-9a-f]{{64}}(")'
        policy, count = re.subn(pattern, rf"\g<1>{digest}\g<2>", policy)
        assert count == 1
    policy_path.write_text(policy, encoding="utf-8")
    return council


def _make_profiles(home: Path) -> None:
    for profile in PROFILES:
        root = home / ".hermes" / "profiles" / profile
        root.mkdir(parents=True)
        for name in ("config.yaml", "SOUL.md", "state.db"):
            (root / name).write_text("synthetic fixture\n", encoding="utf-8")


def _write_shim(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _environment(
    target: Path,
    council: Path | None,
    *,
    docker: bool = True,
    strict_path: bool = False,
) -> tuple[dict[str, str], Path]:
    home = target / "home"
    home.mkdir(exist_ok=True)
    shims = target / "shims"
    shims.mkdir(exist_ok=True)
    _write_shim(
        shims / "uname",
        "#!/bin/sh\ncase \"${1:-}\" in -s) echo Darwin ;; -m) echo arm64 ;; *) exit 2 ;; esac\n",
    )
    system_python_marker = target / "system-python39-was-selected"
    _write_shim(
        shims / "python3",
        "#!/bin/sh\n"
        "if test \"${1:-}\" = --version; then echo 'Python 3.9.18'; exit 0; fi\n"
        f"printf '%s\\n' selected > '{system_python_marker}'\n"
        "exit 39\n",
    )
    if docker:
        _write_shim(
            shims / "docker",
            "#!/bin/sh\ntest \"${1:-}\" = version || exit 2\nexit 0\n",
        )
    environment = {} if strict_path else os.environ.copy()
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "WPI_COUNCIL_PYTHON",
        "WPI_COUNCIL_INSTALL_ROOT",
        "WPI_COUNCIL_BIN_DIR",
        "WPI_COUNCIL_CONFIG",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_BIN_HOME": str(home / ".local" / "bin"),
            "PATH": f"{shims}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "LANG": "C",
        }
    )
    if council is not None:
        environment["WPI_AI_COUNCIL_ROOT"] = str(council)
    return environment, system_python_marker


def _run_installer(project: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(project / "scripts" / "install-council-operator.sh")],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _tree_identity(root: Path) -> list[tuple[str, int, int, bytes | None]]:
    result: list[tuple[str, int, int, bytes | None]] = []
    for path in sorted((root, *root.rglob("*"))):
        metadata = path.lstat()
        result.append(
            (
                str(path.relative_to(root)),
                stat.S_IMODE(metadata.st_mode),
                metadata.st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
        )
    return result


def test_cases_1_2_4_6_7_11_12_13_install_and_reinstall_in_temporary_project_venv(tmp_path):
    project = _copy_project(tmp_path)
    project_python = _make_temporary_pinned_venv(project)
    council = _make_council(project, tmp_path)
    home = tmp_path / "home"
    _make_profiles(home)
    environment, system_python_marker = _environment(tmp_path, council)
    secret = "installer-fixture-secret-must-never-be-written"
    environment["OPENAI_API_KEY"] = secret
    assert subprocess.check_output(
        [str(tmp_path / "shims" / "python3"), "--version"], text=True
    ).strip() == "Python 3.9.18"
    council_before = _tree_identity(council)

    installed = _run_installer(project, environment)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert not system_python_marker.exists()
    runtime_record = home / ".local" / "share" / "wpi-council" / "operator-python.runtime"
    record = runtime_record.read_text(encoding="utf-8").splitlines()
    assert record[0] == "wpi-council-python-runtime/v1"
    assert record[1] == str(project_python)
    assert record[2].startswith("3.12.")
    assert len(record[3]) == 64
    assert record[4] == str(project)
    assert stat.S_IMODE(runtime_record.stat().st_mode) == 0o600

    config = home / ".config" / "wpi-virtual-lab" / "council.toml"
    assert config.is_file()
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    launcher = home / ".local" / "bin" / "wpi-council"
    assert launcher.is_symlink()
    clean_environment = {
        "HOME": str(home),
        "PATH": f"{launcher.parent}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "LANG": "C",
    }
    launched = subprocess.run(
        ["wpi-council", "--help"],
        env=clean_environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert launched.returncode == 0, launched.stdout + launched.stderr
    assert "VIRTUAL_ENV" not in clean_environment
    assert council_before == _tree_identity(council)
    assert all(secret.encode() not in path.read_bytes() for path in home.rglob("*") if path.is_file())

    config.write_text("# preserved untracked operator config\n", encoding="utf-8")
    config.chmod(0o600)
    config_before = config.read_bytes()
    record_before = runtime_record.read_bytes()
    record_mtime_before = runtime_record.stat().st_mtime_ns
    reinstalled = _run_installer(project, environment)
    assert reinstalled.returncode == 0, reinstalled.stdout + reinstalled.stderr
    assert "Existing valid operator Python installation preserved" in reinstalled.stdout
    assert config.read_bytes() == config_before
    assert runtime_record.read_bytes() == record_before
    assert runtime_record.stat().st_mtime_ns == record_mtime_before
    assert council_before == _tree_identity(council)


def test_existing_dedicated_runtime_is_preserved_when_project_venv_appears(tmp_path):
    project, home, dedicated_python, environment, runtime_record = (
        _existing_dedicated_runtime_fixture(tmp_path)
    )
    record_before = runtime_record.read_bytes()
    record_mtime_before = runtime_record.stat().st_mtime_ns
    dedicated_before = _tree_identity(dedicated_python.parents[1])

    result = _run_installer(project, environment)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Existing valid operator Python installation preserved: {dedicated_python}" in result.stdout
    assert f"Recorded operator Python 3.12: {dedicated_python}" in result.stdout
    assert runtime_record.read_bytes() == record_before
    assert runtime_record.stat().st_mtime_ns == record_mtime_before
    assert _tree_identity(dedicated_python.parents[1]) == dedicated_before

    launcher = home / ".local" / "bin" / "wpi-council"
    launched = subprocess.run(
        [str(launcher), "--help"],
        env={
            "HOME": str(home),
            "PATH": f"{launcher.parent}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "LANG": "C",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert launched.returncode == 0, launched.stdout + launched.stderr


def test_existing_dedicated_runtime_is_bootstrap_authority_without_fresh_python312(tmp_path):
    project, _home, dedicated_python, environment, runtime_record = (
        _existing_dedicated_runtime_fixture(tmp_path, create_project_venv=False)
    )
    assert not (project / ".venv").exists()
    assert shutil.which("python3.12", path=environment["PATH"]) is None
    record_before = runtime_record.read_bytes()
    record_mtime_before = runtime_record.stat().st_mtime_ns
    dedicated_before = _tree_identity(dedicated_python.parents[1])

    result = _run_installer(project, environment)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Existing valid operator Python installation preserved: {dedicated_python}" in result.stdout
    assert f"Recorded operator Python 3.12: {dedicated_python}" in result.stdout
    assert runtime_record.read_bytes() == record_before
    assert runtime_record.stat().st_mtime_ns == record_mtime_before
    assert _tree_identity(dedicated_python.parents[1]) == dedicated_before
    assert not (tmp_path / "system-python39-was-selected").exists()


def test_explicit_python_must_match_existing_dedicated_runtime(tmp_path):
    project, home, _dedicated_python, environment, runtime_record = (
        _existing_dedicated_runtime_fixture(tmp_path)
    )
    record_before = runtime_record.read_bytes()
    record_mtime_before = runtime_record.stat().st_mtime_ns
    environment["WPI_COUNCIL_PYTHON"] = str(project / ".venv" / "bin" / "python")

    result = _run_installer(project, environment)

    assert result.returncode != 0
    assert "Existing operator installation uses a different Python; it was preserved." in result.stderr
    assert runtime_record.read_bytes() == record_before
    assert runtime_record.stat().st_mtime_ns == record_mtime_before
    assert not (home / ".config" / "wpi-virtual-lab" / "council.toml").exists()
    assert not (home / ".local" / "bin" / "wpi-council").exists()


def test_case_3_missing_python_312_has_one_exact_installation_instruction(tmp_path):
    project = _copy_project(tmp_path)
    environment, _marker = _environment(tmp_path, None, docker=False, strict_path=True)
    result = _run_installer(project, environment)
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == (
        "No trusted Python 3.12 interpreter was found. Install Python 3.12, then run: "
        f'/absolute/path/to/python3.12 -m venv "{project}/.venv"\n'
    )


def test_case_5_existing_incompatible_installation_is_preserved(tmp_path):
    project = _copy_project(tmp_path)
    council = _make_council(project, tmp_path)
    home = tmp_path / "home"
    _make_profiles(home)
    environment, _marker = _environment(tmp_path, council)
    environment["WPI_COUNCIL_PYTHON"] = sys.executable
    record = home / ".local" / "share" / "wpi-council" / "operator-python.runtime"
    record.parent.mkdir(parents=True)
    incompatible = "wpi-council-python-runtime/v1\n/invalid/python\n3.13.0\n" + "0" * 64 + f"\n{project}\n"
    record.write_text(incompatible, encoding="utf-8")
    record.chmod(0o600)

    result = _run_installer(project, environment)
    assert result.returncode != 0
    assert "Existing operator installation is incompatible; it was preserved." in result.stderr
    assert record.read_text(encoding="utf-8") == incompatible
    assert not (home / ".config" / "wpi-virtual-lab" / "council.toml").exists()


def test_case_8_exact_council_hash_mismatch_blocks_without_modifying_runtime(tmp_path):
    project = _copy_project(tmp_path)
    council = _make_council(project, tmp_path)
    (council / "AGENTS.md").write_text("tampered synthetic contract\n", encoding="utf-8")
    council_before = _tree_identity(council)
    environment, _marker = _environment(tmp_path, council)
    environment["WPI_COUNCIL_PYTHON"] = sys.executable

    result = _run_installer(project, environment)
    assert result.returncode != 0
    assert "hash mismatch" in result.stderr
    assert council_before == _tree_identity(council)
    assert not (tmp_path / "home" / ".local" / "share" / "wpi-council").exists()


def test_case_9_missing_profile_blocks_before_installation(tmp_path):
    project = _copy_project(tmp_path)
    council = _make_council(project, tmp_path)
    home = tmp_path / "home"
    _make_profiles(home)
    (home / ".hermes" / "profiles" / "council-reviewer" / "SOUL.md").unlink()
    environment, _marker = _environment(tmp_path, council)
    environment["WPI_COUNCIL_PYTHON"] = sys.executable

    result = _run_installer(project, environment)
    assert result.returncode != 0
    assert "Hermes profile file is missing: council-reviewer/SOUL.md" in result.stderr
    assert not (home / ".local" / "share" / "wpi-council").exists()


def test_case_10_missing_docker_blocks_before_installation(tmp_path):
    project = _copy_project(tmp_path)
    environment, _marker = _environment(tmp_path, None, docker=False)
    environment["WPI_COUNCIL_PYTHON"] = sys.executable

    result = _run_installer(project, environment)
    assert result.returncode != 0
    assert result.stderr == "Docker is required.\n"
    assert not (tmp_path / "home" / ".local" / "share" / "wpi-council").exists()


def test_resolver_uses_absolute_python312_fallback_and_rejects_relative_override(tmp_path):
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    resolver = scripts / "resolve-council-python.sh"
    shutil.copy2(ROOT / "scripts" / "resolve-council-python.sh", resolver)
    fallback_dir = tmp_path / "fallback"
    fallback_dir.mkdir()
    (fallback_dir / "python3.12").symlink_to(sys.executable)
    environment = os.environ.copy()
    environment.pop("WPI_COUNCIL_PYTHON", None)
    environment["PATH"] = f"{fallback_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"

    selected = subprocess.run(
        [str(resolver), "--project-root", str(project)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert selected.returncode == 0
    assert Path(selected.stdout.strip()).is_absolute()
    assert selected.stdout.strip() == str(fallback_dir / "python3.12")

    environment["WPI_COUNCIL_PYTHON"] = "python3.12"
    rejected = subprocess.run(
        [str(resolver), "--project-root", str(project)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "No trusted Python 3.12 interpreter was found" in rejected.stderr


def test_bootstrap_never_requests_user_or_sudo_installation():
    source = (ROOT / "scripts" / "install-council-operator.sh").read_text(encoding="utf-8")
    assert "pip install --user" not in source
    assert "--user" not in source
    assert "sudo" not in source
    assert "requirements-dev.txt" in source
    assert "constraints-py312.txt" in source


@pytest.mark.parametrize("hostile_kind", ["world_writable", "symlink_parent"])
def test_installer_rejects_unsafe_user_local_install_directory(tmp_path, hostile_kind):
    project = _copy_project(tmp_path)
    _make_temporary_pinned_venv(project)
    council = _make_council(project, tmp_path)
    home = tmp_path / "home"
    _make_profiles(home)
    environment, _marker = _environment(tmp_path, council)

    if hostile_kind == "world_writable":
        install_root = home / "shared-install"
        install_root.mkdir()
        install_root.chmod(0o777)
    else:
        outside = home / "real-install-parent"
        outside.mkdir(mode=0o700)
        linked = home / "linked-install-parent"
        linked.symlink_to(outside, target_is_directory=True)
        install_root = linked / "operator"
    environment["WPI_COUNCIL_INSTALL_ROOT"] = str(install_root)

    result = _run_installer(project, environment)
    assert result.returncode != 0
    assert "user-local" in result.stderr
    assert not (install_root / "operator-python.runtime").exists()
