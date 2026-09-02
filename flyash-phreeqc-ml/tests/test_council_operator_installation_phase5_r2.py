from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import zipfile

import pytest

import test_council_operator_installation_phase5_r1 as r1


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_FAILURE = (
    "The selected Python 3.12 environment has no usable local pip and its "
    "standard-library ensurepip bootstrap failed. Repair ensurepip for this exact "
    "interpreter, then rerun the installer."
)


@dataclass(frozen=True)
class PiplessFixture:
    project: Path
    python: Path
    home: Path
    environment: dict[str, str]
    system_python_marker: Path
    child_site: Path
    wheelhouse: Path


def _build_local_setuptools_wheel(wheelhouse: Path) -> Path:
    """Repack the already-verified test environment; never contact an index."""

    distribution = metadata.distribution("setuptools")
    assert distribution.version == "83.0.0"
    wheelhouse.mkdir()
    wheel = wheelhouse / "setuptools-83.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for packaged in distribution.files or ():
            relative = Path(str(packaged))
            if relative.is_absolute() or ".." in relative.parts:
                continue
            source = Path(distribution.locate_file(packaged))
            if source.is_file():
                archive.write(source, relative.as_posix())
    assert wheel.is_file() and wheel.stat().st_size > 0
    return wheel


def _approved_dependency_mirror(parent_site: Path, target: Path) -> None:
    """Expose pinned test dependencies without exposing parent pip/setuptools."""

    target.mkdir()
    excluded_prefixes = (
        "_distutils_hack",
        "pip",
        "pkg_resources",
        "setuptools",
    )
    for source in parent_site.iterdir():
        lowered = source.name.lower()
        if (
            lowered.startswith(excluded_prefixes)
            or lowered == "distutils-precedence.pth"
            or lowered.endswith((".egg-link", ".pth"))
            or lowered in {"__pycache__", "flyash_phreeqc_ml"}
        ):
            continue
        (target / source.name).symlink_to(source, target_is_directory=source.is_dir())


def _seed_marker_pip(site_root: Path, marker: Path) -> None:
    package = site_root / "pip"
    package.mkdir(parents=True)
    source = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('contaminating pip executed\\n', encoding='utf-8')\n"
        "raise RuntimeError('contaminating pip must not execute')\n"
    )
    (package / "__init__.py").write_text(source, encoding="utf-8")
    (package / "__main__.py").write_text(source, encoding="utf-8")


def _make_genuinely_pipless_fixture(tmp_path: Path) -> PiplessFixture:
    assert sys.version_info[:2] == (3, 12)
    project = r1._copy_project(tmp_path)
    helper = project / "scripts" / "bootstrap_council_pip.py"
    if not helper.exists():
        shutil.copy2(ROOT / "scripts" / helper.name, helper)
        helper.chmod(0o755)

    python = project / ".venv" / "bin" / "python"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(project / ".venv")],
        check=True,
        capture_output=True,
        text=True,
    )
    child_site = Path(
        subprocess.check_output(
            [str(python), "-I", "-c", "import site; print(site.getsitepackages()[0])"],
            text=True,
        ).strip()
    )
    parent_site = Path(
        subprocess.check_output(
            [sys.executable, "-I", "-c", "import site; print(site.getsitepackages()[0])"],
            text=True,
        ).strip()
    )
    approved_site = project / ".r2-approved-dependencies"
    _approved_dependency_mirror(parent_site, approved_site)
    (child_site / "wpi-r2-approved-dependencies.pth").write_text(
        "import os,sys; sys.path.append("
        + repr(str(approved_site))
        + ") if os.path.isdir("
        + repr(str(child_site / "pip"))
        + ") else None\n",
        encoding="utf-8",
    )
    wheelhouse = project / ".r2-wheelhouse"
    _build_local_setuptools_wheel(wheelhouse)

    council = r1._make_council(project, tmp_path)
    home = tmp_path / "home"
    r1._make_profiles(home)
    environment, system_python_marker = r1._environment(tmp_path, council)
    environment.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_FIND_LINKS": str(wheelhouse),
            "PIP_NO_INDEX": "1",
            "PIP_REQUIRE_VIRTUALENV": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )

    unavailable = subprocess.run(
        [str(python), "-I", "-m", "pip", "--version"],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert unavailable.returncode != 0
    assert not (child_site / "pip").exists()
    return PiplessFixture(
        project=project,
        python=python,
        home=home,
        environment=environment,
        system_python_marker=system_python_marker,
        child_site=child_site,
        wheelhouse=wheelhouse,
    )


def _run_helper(fixture: PiplessFixture) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(fixture.python), str(fixture.project / "scripts" / "bootstrap_council_pip.py")],
        cwd=fixture.project,
        env=fixture.environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _stdlib_ensurepip_identity(fixture: PiplessFixture) -> dict[str, str]:
    result = subprocess.run(
        [
            str(fixture.python),
            "-I",
            "-c",
            """import ensurepip
import json
from pathlib import Path
import sysconfig

module = Path(ensurepip.__file__).resolve()
stdlib = Path(sysconfig.get_path("stdlib")).resolve()
if module != stdlib and stdlib not in module.parents:
    raise SystemExit(1)
print(json.dumps({"ensurepip_module": str(module), "stdlib_root": str(stdlib)}, sort_keys=True))
""",
        ],
        cwd=fixture.project,
        env=fixture.environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _run_instrumented_helper_with_existing_pip(
    fixture: PiplessFixture,
    marker: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(fixture.python),
            "-I",
            "-c",
            """import importlib.util
from pathlib import Path
import sys

helper_path = Path(sys.argv[1])
marker = Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("wpi_test_bootstrap_council_pip", helper_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
original = module._run_interpreter

def observed(*arguments):
    if arguments == ("-m", "ensurepip", "--default-pip"):
        marker.write_text("ensurepip invoked\\n", encoding="utf-8")
    return original(*arguments)

module._run_interpreter = observed
sys.argv = [str(helper_path)]
raise SystemExit(module.main())
""",
            str(fixture.project / "scripts" / "bootstrap_council_pip.py"),
            str(marker),
        ],
        cwd=fixture.project,
        env=fixture.environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _helper_evidence(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""
    evidence = json.loads(result.stdout)
    assert isinstance(evidence, dict)
    return evidence


def _state_paths(fixture: PiplessFixture) -> tuple[Path, ...]:
    install_root = fixture.home / ".local" / "share" / "wpi-council"
    return (
        fixture.home / ".config" / "wpi-virtual-lab" / "council.toml",
        install_root / "operator-python.runtime",
        install_root / "libexec" / "resolve-council-python.sh",
        install_root / "bin" / "wpi-council",
        fixture.home / ".local" / "bin" / "wpi-council",
    )


def _assert_no_published_operator_state(fixture: PiplessFixture) -> None:
    install_root = fixture.home / ".local" / "share" / "wpi-council"
    for path in _state_paths(fixture):
        assert not path.exists()
        assert not path.is_symlink()
    assert not (install_root / "venv").exists()
    for parent in (
        fixture.home / ".config" / "wpi-virtual-lab",
        fixture.home / ".local" / "share" / "wpi-council",
        fixture.home / ".local" / "bin",
    ):
        if parent.exists():
            assert not list(parent.rglob("*.tmp.*"))


def _installed_identity(fixture: PiplessFixture) -> dict[str, object]:
    checked = subprocess.run(
        [
            str(fixture.python),
            "-I",
            "-c",
            """from importlib import metadata
import json
from pathlib import Path
import flyash_phreeqc_ml
import pip
import sys

distribution = metadata.distribution("flyash-phreeqc-ml")
direct_url = json.loads(distribution.read_text("direct_url.json"))
print(json.dumps({
    "direct_url": direct_url,
    "package_root": str(Path(flyash_phreeqc_ml.__file__).resolve().parent),
    "pip_module": str(Path(pip.__file__).resolve()),
    "prefix": str(Path(sys.prefix).resolve()),
    "setuptools": metadata.version("setuptools"),
}, sort_keys=True))
""",
        ],
        cwd=fixture.project,
        env=fixture.environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    return json.loads(checked.stdout)


def _inject_installer_failure(
    project: Path,
    marker: str,
    stage: str,
    occurrence: int,
) -> None:
    installer = project / "scripts" / "install-council-operator.sh"
    source = installer.read_text(encoding="utf-8")
    positions: list[int] = []
    cursor = 0
    while (position := source.find(marker, cursor)) >= 0:
        positions.append(position)
        cursor = position + len(marker)
    assert len(positions) >= occurrence, stage
    position = positions[occurrence - 1]
    injected = f'  echo "injected R2 {stage} failure" >&2\n  exit 97\n'
    installer.write_text(source[:position] + injected + source[position:], encoding="utf-8")
    installer.chmod(0o755)


def test_genuine_pipless_python_bootstraps_locally_installs_exact_toolchain_and_recovers_empty_dirs(
    tmp_path: Path,
) -> None:
    fixture = _make_genuinely_pipless_fixture(tmp_path)
    config_parent = fixture.home / ".config" / "wpi-virtual-lab"
    install_root = fixture.home / ".local" / "share" / "wpi-council"
    public_bin = fixture.home / ".local" / "bin"
    for directory in (config_parent, install_root, public_bin):
        directory.mkdir(parents=True, exist_ok=True)
        assert not any(directory.iterdir())
    original_inodes = {path: path.stat().st_ino for path in (config_parent, install_root, public_bin)}

    system_version = subprocess.check_output(
        [str(tmp_path / "shims" / "python3"), "--version"], text=True
    ).strip()
    assert system_version == "Python 3.9.18"
    user_site = Path(
        subprocess.check_output(
            [str(fixture.python), "-I", "-c", "import site; print(site.getusersitepackages())"],
            env=fixture.environment,
            text=True,
        ).strip()
    )
    user_site_marker = tmp_path / "user-site-pip-was-executed"
    ambient_marker = tmp_path / "ambient-pythonpath-pip-was-executed"
    ambient_site = tmp_path / "ambient-system-site"
    _seed_marker_pip(user_site, user_site_marker)
    _seed_marker_pip(ambient_site, ambient_marker)
    fixture.environment["PYTHONPATH"] = str(ambient_site)
    ensurepip_identity = _stdlib_ensurepip_identity(fixture)
    assert Path(ensurepip_identity["ensurepip_module"]).is_relative_to(
        Path(ensurepip_identity["stdlib_root"])
    )
    poisoned_requirement = tmp_path / "ambient-requirement.txt"
    poisoned_requirement.write_text(
        "ambient-unapproved-package==999.0.0\n", encoding="utf-8"
    )
    poisoned_constraint = tmp_path / "ambient-constraint.txt"
    poisoned_constraint.write_text("setuptools==82.0.0\n", encoding="utf-8")
    fixture.environment.update(
        {
            "PIP_CONSTRAINT": str(poisoned_constraint),
            "PIP_EDITABLE": str(tmp_path / "ambient-editable-project"),
            "PIP_REQUIREMENT": str(poisoned_requirement),
        }
    )
    installed = r1._run_installer(fixture.project, fixture.environment)

    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert '"bootstrapped":true' in installed.stdout
    assert '"version":"83.0.0"' in installed.stdout
    assert not fixture.system_python_marker.exists()
    assert not user_site_marker.exists()
    assert not ambient_marker.exists()
    assert {path: path.stat().st_ino for path in original_inodes} == original_inodes
    assert not (install_root / "venv").exists()
    assert all(path.exists() or path.is_symlink() for path in _state_paths(fixture))

    identity = _installed_identity(fixture)
    prefix = Path(str(identity["prefix"]))
    assert prefix == fixture.python.parents[1].resolve()
    assert Path(str(identity["pip_module"])).is_relative_to(prefix)
    assert identity["setuptools"] == "83.0.0"
    assert Path(str(identity["package_root"])) == (fixture.project / "flyash_phreeqc_ml").resolve()
    direct_url = identity["direct_url"]
    assert isinstance(direct_url, dict)
    assert direct_url.get("dir_info", {}).get("editable") is True
    assert direct_url.get("url", "").endswith(fixture.project.name)

    pip_evidence = _helper_evidence(_run_helper(fixture))
    assert pip_evidence["bootstrapped"] is False
    assert Path(str(pip_evidence["pip_module"])).is_relative_to(prefix)
    assert Path(str(pip_evidence["pip_distribution_root"])).is_relative_to(prefix)

    lock_check = subprocess.run(
        [str(fixture.python), str(fixture.project / "scripts" / "validate_dependency_lock.py"), "--installed"],
        cwd=fixture.project,
        env=fixture.environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert lock_check.returncode == 0, lock_check.stdout + lock_check.stderr


def test_existing_local_pip_is_preserved_without_reinvoking_ensurepip(tmp_path: Path) -> None:
    fixture = _make_genuinely_pipless_fixture(tmp_path)
    first = _helper_evidence(_run_helper(fixture))
    assert first["bootstrapped"] is True
    assert Path(str(first["ensurepip_module"])).is_relative_to(Path(str(first["stdlib_root"])))
    second = _helper_evidence(_run_helper(fixture))
    assert second["bootstrapped"] is False
    for key in ("environment_prefix", "pip_distribution_root", "pip_module", "pip_version", "python_executable"):
        assert second[key] == first[key]
    ensurepip_marker = tmp_path / "ensurepip-was-invoked"
    instrumented = _helper_evidence(
        _run_instrumented_helper_with_existing_pip(fixture, ensurepip_marker)
    )
    assert instrumented["bootstrapped"] is False
    assert not ensurepip_marker.exists()

    installed = r1._run_installer(fixture.project, fixture.environment)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert '"bootstrapped":false' in installed.stdout
    after = _helper_evidence(_run_helper(fixture))
    assert after["bootstrapped"] is False
    assert after["pip_module"] == first["pip_module"]
    assert after["pip_version"] == first["pip_version"]
    assert not fixture.system_python_marker.exists()


def test_wrong_or_missing_setuptools_fails_before_exact_build_requirement_install(tmp_path: Path) -> None:
    fixture = _make_genuinely_pipless_fixture(tmp_path)
    evidence = _helper_evidence(_run_helper(fixture))
    assert evidence["bootstrapped"] is True
    wrong = fixture.child_site / "setuptools-82.0.0.dist-info"
    wrong.mkdir()
    (wrong / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: setuptools\nVersion: 82.0.0\n",
        encoding="utf-8",
    )
    invalid = subprocess.run(
        [str(fixture.python), str(fixture.project / "scripts" / "validate_dependency_lock.py"), "--installed"],
        cwd=fixture.project,
        env=fixture.environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode != 0
    assert "installed: setuptools is 82.0.0, expected 83.0.0" in invalid.stderr


def test_editable_no_build_isolation_fails_without_setuptools_then_full_install_succeeds(
    tmp_path: Path,
) -> None:
    fixture = _make_genuinely_pipless_fixture(tmp_path)
    _helper_evidence(_run_helper(fixture))
    premature = subprocess.run(
        [
            str(fixture.python),
            "-I",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-build-isolation",
            "--no-deps",
            "--editable",
            str(fixture.project),
        ],
        cwd=fixture.project,
        env=fixture.environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert premature.returncode != 0
    assert "setuptools.build_meta" in premature.stderr
    _assert_no_published_operator_state(fixture)

    installed = r1._run_installer(fixture.project, fixture.environment)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert _installed_identity(fixture)["setuptools"] == "83.0.0"


def test_ensurepip_failure_is_deterministic_and_publishes_no_operator_state(tmp_path: Path) -> None:
    fixture = _make_genuinely_pipless_fixture(tmp_path)
    original_mode = stat.S_IMODE(fixture.child_site.stat().st_mode)
    fixture.child_site.chmod(0o500)
    try:
        failed = r1._run_installer(fixture.project, fixture.environment)
    finally:
        fixture.child_site.chmod(original_mode)

    assert failed.returncode != 0
    assert failed.stdout
    assert failed.stderr == BOOTSTRAP_FAILURE + "\n"
    assert not fixture.system_python_marker.exists()
    _assert_no_published_operator_state(fixture)


@pytest.mark.parametrize(
    ("stage", "marker", "occurrence"),
    [
        (
            "dependency installation",
            "  selected_operator_python -m pip install --disable-pip-version-check \\",
            1,
        ),
        (
            "setuptools verification",
            "  setuptools_identity=$(verified_setuptools_identity) || fail",
            2,
        ),
        (
            "editable installation",
            "  selected_operator_python -m pip install --disable-pip-version-check \\",
            2,
        ),
        (
            "installed-pin validation",
            '  selected_operator_python "$project_dir/scripts/validate_dependency_lock.py" --installed\n',
            1,
        ),
    ],
)
def test_install_failures_before_publication_leave_no_config_runtime_resolver_or_launcher(
    tmp_path: Path,
    stage: str,
    marker: str,
    occurrence: int,
) -> None:
    fixture = _make_genuinely_pipless_fixture(tmp_path)
    _inject_installer_failure(fixture.project, marker, stage, occurrence)
    failed = r1._run_installer(fixture.project, fixture.environment)

    assert failed.returncode == 97, failed.stdout + failed.stderr
    assert f"injected R2 {stage} failure" in failed.stderr
    assert not fixture.system_python_marker.exists()
    _assert_no_published_operator_state(fixture)


def test_new_dedicated_operator_venv_is_removed_when_install_fails_before_publication(
    tmp_path: Path,
) -> None:
    project = r1._copy_project(tmp_path)
    council = r1._make_council(project, tmp_path)
    home = tmp_path / "home"
    r1._make_profiles(home)
    environment, system_python_marker = r1._environment(tmp_path, council)
    base_python = Path(
        subprocess.check_output(
            [sys.executable, "-I", "-c", "import sys; print(sys._base_executable)"],
            text=True,
        ).strip()
    )
    base_check = subprocess.run(
        [
            str(base_python),
            "-I",
            "-c",
            "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) and sys.prefix == sys.base_prefix else 1)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert base_check.returncode == 0, base_check.stdout + base_check.stderr
    environment["WPI_COUNCIL_PYTHON"] = str(base_python)
    _inject_installer_failure(
        project,
        "  selected_operator_python -m pip install --disable-pip-version-check \\",
        "dedicated environment dependency installation",
        1,
    )
    fixture = PiplessFixture(
        project=project,
        python=base_python,
        home=home,
        environment=environment,
        system_python_marker=system_python_marker,
        child_site=home / ".local" / "share" / "wpi-council" / "venv" / "lib",
        wheelhouse=project / ".unused-wheelhouse",
    )

    failed = r1._run_installer(project, environment)

    assert failed.returncode == 97, failed.stdout + failed.stderr
    assert "injected R2 dedicated environment dependency installation failure" in failed.stderr
    _assert_no_published_operator_state(fixture)
    install_root = home / ".local" / "share" / "wpi-council"
    assert install_root.is_dir()
    assert not any(install_root.iterdir())
    assert not system_python_marker.exists()


def test_bootstrap_source_and_installer_preserve_trusted_exact_build_contract() -> None:
    helper = (ROOT / "scripts" / "bootstrap_council_pip.py").read_text(encoding="utf-8")
    installer = (ROOT / "scripts" / "install-council-operator.sh").read_text(encoding="utf-8")
    for forbidden in ("get-pip.py", "curl", "urlopen", "--user", "sudo"):
        assert forbidden not in helper
    assert '[sys.executable, "-I", *arguments]' in helper
    assert '_run_interpreter("-m", "ensurepip", "--default-pip")' in helper
    assert "selected_operator_python -m pip install --disable-pip-version-check" in installer
    for unsafe_input in (
        "PIP_CONSTRAINT",
        "PIP_EDITABLE",
        "PIP_PREFIX",
        "PIP_REQUIREMENT",
        "PIP_ROOT",
        "PIP_TARGET",
        "PIP_USER",
    ):
        assert f"-u {unsafe_input}" in installer
    assert "setuptools==83.0.0 \\" in installer
    assert '--requirement "$project_dir/requirements-dev.txt"' in installer
    assert '--no-build-isolation --no-deps --editable "$project_dir"' in installer
    assert installer.index("setuptools==83.0.0") < installer.index("--no-build-isolation")


def test_phase5_ci_runs_the_functional_r2_file_and_emits_proof_only_after_success() -> None:
    workflow_path = Path(
        os.environ.get(
            "WPI_PHASE5_CI_WORKFLOW",
            ROOT.parent / ".github" / "workflows" / "ci.yml",
        )
    )
    workflow = workflow_path.read_text(encoding="utf-8")
    test_name = "tests/test_council_operator_installation_phase5_r2.py"
    assert test_name in workflow
    assert "Functionally verify the Phase 5-R2 pipless bootstrap correction" in workflow
    test_position = workflow.index(test_name)
    for marker in (
        "PIPLESS_PY312_BOOTSTRAP_PASS=True",
        "ENSUREPIP_IS_LOCAL_AND_TRUSTED=True",
        "SYSTEM_PYTHON_NOT_USED=True",
        "PINNED_SETUPTOOLS_83_INSTALLED=True",
        "PARTIAL_INSTALL_RECOVERY_PASS=True",
        "FAILED_BOOTSTRAP_PUBLISHES_NO_OPERATOR_STATE=True",
    ):
        assert workflow.index(marker) > test_position
