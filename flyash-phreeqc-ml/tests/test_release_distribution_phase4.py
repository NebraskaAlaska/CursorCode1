from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_SHA = "b5c4a6dfea1a6bb6a3436857a50346bb943904a49582714494b4f1b1e54e64e1"
DATABASE_SHA = "59373961d648dfbf68a40744060c1d64f57ecbec98f4f5fb89f3a1b4213ccd10"
NOTICE_SHA = "f5c97c1c0fea8c27096d3e7b298a627264a71683dc2debb374d07baaea168b34"
DEBIAN_IMAGE_SHA = "88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171"
PYTHON_IMAGE_SHA = "fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db"
NGINX_IMAGE_SHA = "4ff102c5d78d254a6f0da062b3cf39eaf07f01eec0927fd21e219d0af8bc0591"
OLLAMA_IMAGE_SHA = "a5409cb903d30f9cd67e9f430dd336ddc9274e16fd78f75b675c42065991b4fd"


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def workflow_text(name: str) -> str | None:
    path = ROOT.parent / ".github" / "workflows" / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    # The Docker build context is intentionally the package directory, so
    # repo-level workflow metadata is absent from the test image. Source CI
    # executes these assertions before building that image.
    assert os.environ.get("APP_IN_DOCKER") == "1"
    return None


@pytest.mark.release
def test_dockerfile_pins_official_runtime_and_stable_layout() -> None:
    dockerfile = text("Dockerfile")
    assert "PHREEQC_VERSION=3.8.6-17100" in dockerfile
    assert f"debian:bookworm-slim@sha256:{DEBIAN_IMAGE_SHA}" in dockerfile
    assert dockerfile.count(f"python:3.12.10-slim-bookworm@sha256:{PYTHON_IMAGE_SHA}") == 2
    assert "https://water.usgs.gov/water-resources/software/PHREEQC/phreeqc-3.8.6-17100.tar.gz" in dockerfile
    assert "--retry 12 --retry-all-errors --retry-delay 2 --retry-max-time 900" in dockerfile
    assert dockerfile.count('while [ "$attempt" -le 6 ]') == 2
    assert dockerfile.count('sleep "$((attempt * 2))"') == 2
    assert ARCHIVE_SHA in dockerfile
    assert DATABASE_SHA in dockerfile
    assert "PHREEQC_DATABASE=/opt/phreeqc/database/phreeqc.dat" in dockerfile
    assert "/opt/phreeqc/share/examples/ex1" in dockerfile
    assert "FROM runtime AS test" in dockerfile
    assert "FROM runtime AS release" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "Private-beta" not in dockerfile
    assert '"manifest_schema": "wpi.virtual-lab.scientific-resource"' in dockerfile
    assert '"resource_kind": "phreeqc_runtime"' in dockerfile
    assert '"test_status": "passed"' in dockerfile
    assert '"standalone_test_status": "passed"' in dockerfile
    assert "COPY --chown=root:root resources ./resources" in dockerfile
    assert "COPY data/raw" not in dockerfile
    assert "VLAB_IMPORT_ROOT=/app/imports" in dockerfile
    assert not re.search(r"(?m)^COPY\s+\.\s", dockerfile)


@pytest.mark.release
def test_phreeqc_manifest_notice_and_blank_template_are_deterministic() -> None:
    manifest = json.loads(text("release/PHREEQC_SOURCE_MANIFEST.json"))
    assert manifest["version"] == "3.8.6-17100"
    assert manifest["archive_sha256"] == ARCHIVE_SHA
    assert manifest["database"]["sha256"] == DATABASE_SHA
    assert manifest["database"]["runtime_path"] == "/opt/phreeqc/database/phreeqc.dat"
    notice = (ROOT / "release/PHREEQC_USGS_RIGHTS_NOTICE.txt").read_bytes()
    assert hashlib.sha256(notice).hexdigest() == NOTICE_SHA
    template = text("release/templates/SYNTHETIC_DEMO_icp_template.csv")
    assert template.count("\n") == 1
    assert template.startswith("sample_id,")


@pytest.mark.release
def test_no_raw_data_can_enter_any_image_target() -> None:
    dockerignore = text(".dockerignore").splitlines()
    assert "data/raw" in dockerignore
    assert not any(line.startswith("!data/raw") for line in dockerignore)
    assert "data/raw" not in text("Dockerfile")
    entrypoint = text("docker-entrypoint.sh")
    assert "bootstrap-release" in entrypoint
    assert "--database-dir /opt/phreeqc/database" in entrypoint
    assert "VLAB_ALLOWED_SUBJECTS" in entrypoint
    assert "VLAB_MEMBER_SUBJECTS" in entrypoint
    assert "VLAB_VIEWER_SUBJECTS" in entrypoint
    assert "/app/.streamlit/secrets.toml" in entrypoint


@pytest.mark.release
def test_compose_local_and_server_boundaries() -> None:
    local = text("docker-compose.local.yml")
    server = text("docker-compose.server.yml")
    compatibility = text("docker-compose.yml")
    assert all("init: true" in compose for compose in (local, server, compatibility))
    assert '127.0.0.1:${WPI_PORT:-8501}:8501' in local
    assert "VLAB_AI_PROVIDER: ${VLAB_AI_PROVIDER:-disabled}" in local
    for root in (
        "/app/outputs",
        "/app/experiments",
        "/app/data/processed",
        "/app/imports",
        "/var/lib/wpi/resources",
    ):
        assert root in local and root in server
    assert "VLAB_DEPLOYMENT_MODE: hosted" in server
    assert "VLAB_IMPORT_ROOT: /app/imports" in local
    assert "VLAB_IMPORT_ROOT: /app/imports" in server
    assert "VLAB_ALLOWED_SUBJECTS: ${VLAB_ALLOWED_SUBJECTS:-}" in server
    assert "VLAB_MEMBER_SUBJECTS:" in server
    assert "VLAB_MEMBER_TENANTS:" in server
    assert "VLAB_VIEWER_SUBJECTS:" in server
    assert "VLAB_VIEWER_TENANTS:" in server
    assert "VLAB_STREAMLIT_SECRETS_FILE:?" in server
    assert "WPI_AUTH_FILE:?" in server
    assert "WPI_TLS_CERT_FILE:?" in server
    assert "WPI_TLS_KEY_FILE:?" in server
    assert "internal: true" in server
    assert f"nginx:1.27.4-alpine@sha256:{NGINX_IMAGE_SHA}" in server
    assert f"ollama/ollama:0.11.10@sha256:{OLLAMA_IMAGE_SHA}" in server
    assert "mem_limit:" in server and "pids_limit:" in server and "cpus:" in server
    proxy_block = server.split("  proxy:", 1)[1].split("  ollama:", 1)[0]
    assert 'user: "101:101"' in proxy_block
    assert "uid=101,gid=101,mode=0700" in proxy_block
    assert "cap_drop:\n      - ALL" in proxy_block
    assert not text("deploy/nginx.conf").startswith("user ")
    assert "PHREEQC_CONTAINER_IMAGE_DIGEST: ${WPI_IMAGE_DIGEST:-}" in local
    assert "PHREEQC_CONTAINER_IMAGE_DIGEST: ${WPI_IMAGE_DIGEST:-}" in server
    assert "REPLACE_WITH" not in local and "REPLACE_WITH" not in server
    app_block = server.split("  proxy:", 1)[0]
    ollama_block = server.split("  ollama:", 1)[1].split("networks:", 1)[0]
    assert not re.search(r"(?m)^    ports:", app_block)
    assert not re.search(r"(?m)^    ports:", ollama_block)


@pytest.mark.release
def test_hosted_operations_document_test_target_and_compose_selected_backups() -> None:
    hosted = text("docs/hosted_deployment.md")
    assert "--target test" in hosted
    assert "wpi-virtual-lab:phase4-test" in hosted
    assert "exec -T app python -m pytest" not in hosted
    assert "mandatory SHA-256 sidecar" in hosted
    for path in (
        "scripts/backup-local.sh",
        "scripts/restore-local.sh",
        "scripts/backup-local.ps1",
        "scripts/restore-local.ps1",
    ):
        assert "WPI_COMPOSE_FILE" in text(path)


@pytest.mark.release
def test_container_import_root_repoints_legacy_icp_default(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment["VLAB_IMPORT_ROOT"] = str(tmp_path / "imports")
    completed = subprocess.run(
        [sys.executable, "-c", (
            "from flyash_phreeqc_ml import config; "
            "print(config.EXPERIMENTAL_ICP_DIR)"
        )],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert Path(completed.stdout.strip()) == tmp_path / "imports" / "experimental_icp"


@pytest.mark.release
def test_dependency_declarations_are_exact_and_agree() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/validate_dependency_lock.py"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    for path in ("requirements.txt", "constraints-py312.txt"):
        for raw in text(path).splitlines():
            line = raw.split("#", 1)[0].strip()
            if line and not line.startswith("-r "):
                assert re.fullmatch(
                    r"[A-Za-z0-9_.-]+==[^\s;]+(?:\s*;\s*.+)?", line
                )
    constraints = text("constraints-py312.txt")
    assert 'watchdog==6.0.0 ; platform_system != "Darwin"' in constraints
    assert 'colorama==0.4.6 ; sys_platform == "win32"' in constraints
    assert 'tzdata==2026.3 ; sys_platform == "win32"' in constraints


@pytest.mark.release
def test_ci_has_visible_pinned_vulnerability_and_clean_environment_checks() -> None:
    workflow = workflow_text("ci.yml")
    if workflow is None:
        return
    audit = text("scripts/audit_dependencies.py")
    assert "python -m venv /tmp/wpi-phase4-ci-venv" in workflow
    assert "pip-audit==2.10.1" in workflow
    assert "python scripts/audit_dependencies.py" in workflow
    assert 'PIP_AUDIT_VERSION = "2.10.1"' in audit
    assert "return 2" in audit
    assert "--no-deps" in audit and "--disable-pip" in audit and "--strict" in audit


@pytest.mark.release
def test_ci_executes_phreeqc_contracts_on_each_release_architecture() -> None:
    ci = workflow_text("ci.yml")
    release = workflow_text("release-candidate.yml")
    if ci is None or release is None:
        return
    for workflow in (ci, release):
        assert "linux/amd64" in workflow and "linux/arm64" in workflow
        assert "PHREEQC_INTEGRATION_REQUIRED=1" in workflow
        assert "tests/test_phreeqc_runtime_phase4.py" in workflow
        assert "tests/test_phreeqc_executor.py" in workflow
        assert "tests/test_phreeqc_runner.py" in workflow
        assert "tests/test_phreeqc_run_contract.py" in workflow
        assert "tests/test_resource_steward_phase4.py" in workflow
        assert "docker run --rm --init" in workflow
        assert "-p no:cacheprovider" in workflow
    assert "per-architecture-phreeqc-integration:linux-amd64-linux-arm64" in release


@pytest.mark.release
def test_ci_official_phreeqc_example_uses_writable_nonroot_workdir() -> None:
    workflow = workflow_text("ci.yml")
    if workflow is None:
        return
    assert "docker run --rm --init --workdir /tmp --entrypoint /bin/sh" in workflow


@pytest.mark.release
def test_ci_has_a_fail_closed_aggregate_release_gate() -> None:
    workflow = workflow_text("ci.yml")
    if workflow is None:
        return
    assert "phase4-release-gate:" in workflow
    assert "if: ${{ always() }}" in workflow
    for required_job in (
        "python-and-release-contracts",
        "dependency-vulnerability-audit",
        "container-and-compose",
        "per-architecture-phreeqc-integration",
    ):
        assert f"- {required_job}" in workflow
        assert f"needs.{required_job}.result" in workflow
    assert workflow.count('test "$') >= 4


@pytest.mark.release
def test_direct_ci_container_runs_always_request_an_init_process() -> None:
    for name in ("ci.yml", "release-candidate.yml"):
        workflow = workflow_text(name)
        if workflow is None:
            continue
        direct_runs = [
            line.strip()
            for line in workflow.splitlines()
            if "docker run" in line and not line.lstrip().startswith("#")
        ]
        assert direct_runs
        assert all("--init" in line for line in direct_runs), direct_runs


@pytest.mark.release
def test_ci_labels_hosted_success_path_as_simulated_not_live_oidc() -> None:
    workflow = workflow_text("ci.yml")
    if workflow is None:
        return
    assert "Exercise simulated hosted OIDC success path through Compose" in workflow
    assert "no live IdP exchange claimed" in workflow
    assert '"is_logged_in": True' in workflow
    assert "scope.is_local_compatibility_mode" in workflow


@pytest.mark.release
def test_shared_release_scanner_checks_tree_and_docker_canaries() -> None:
    if shutil.which("git") is None:
        assert os.environ.get("APP_IN_DOCKER") == "1"
        assert "SECRET_PATTERNS" in text("scripts/release_scan.py")
        assert json.loads(text("release/scan-policy.json"))["required_docker_ignored_canaries"]
        return
    probe = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=ROOT, capture_output=True, check=False
    )
    if probe.returncode:
        assert os.environ.get("APP_IN_DOCKER") == "1"
        assert "SECRET_PATTERNS" in text("scripts/release_scan.py")
        assert json.loads(text("release/scan-policy.json"))["required_docker_ignored_canaries"]
        return
    completed = subprocess.run(
        [sys.executable, "scripts/release_scan.py", "--working-tree", "--docker-context"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    hook = text("scripts/hooks/pre-commit")
    assert "scripts/release_scan.py" in hook
    assert "--staged --docker-context" in hook


@pytest.mark.release
@pytest.mark.parametrize(
    "script",
    [
        "launch-local.sh",
        "launch-local.command",
        "stop-local.sh",
        "check-local-install.sh",
        "backup-local.sh",
        "restore-local.sh",
        "run_nsf_demo.sh",
    ],
)
def test_shell_entrypoints_have_offline_help(script: str) -> None:
    completed = subprocess.run(
        ["sh", f"scripts/{script}", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.release
def test_demo_and_release_docs_keep_scientific_status_visible() -> None:
    required = [
        "docs/local_installation.md",
        "docs/hosted_deployment.md",
        "docs/NSF_DEMO_SCRIPT.md",
        "docs/NSF_DEMO_ACCEPTANCE_CHECKLIST.md",
        "docs/NSF_DEMO_TECHNICAL_APPENDIX.md",
        "docs/NSF_DEMO_FALLBACK.md",
    ]
    combined = "\n".join(text(path) for path in required)
    assert "SYNTHETIC DEMO" in combined
    assert "not measured" in combined.lower()
    assert "not validated" in combined.lower()
    assert ARCHIVE_SHA in combined
    assert DATABASE_SHA in combined


@pytest.mark.release
def test_release_manifest_starts_unverified_and_contains_no_secret_fields() -> None:
    manifest = json.loads(text("release/RELEASE_MANIFEST.template.json"))
    serialized = json.dumps(manifest).lower()
    assert manifest["release_status"] == "candidate_unverified"
    assert manifest["git_commit"] is None
    assert manifest["image"]["digest"] is None
    assert manifest["verification"]
    assert all(value == "not_recorded" for value in manifest["verification"].values())
    assert "api_key" not in serialized and "password" not in serialized


@pytest.mark.release
def test_update_monitor_is_metadata_only_and_fails_visibly_on_parser_change(tmp_path: Path) -> None:
    usgs = tmp_path / "usgs.html"
    empa = tmp_path / "empa.html"
    output = tmp_path / "report.json"
    usgs.write_text(
        '<a href="phreeqc-3.8.6-17100.tar.gz">phreeqc-3.8.6-17100.tar.gz</a>',
        encoding="utf-8",
    )
    empa.write_text("Official CEMDATA18.11 PHREEQC release", encoding="utf-8")
    command = [
        sys.executable,
        "scripts/monitor_official_resources.py",
        "--output",
        str(output),
        "--usgs-fixture",
        str(usgs),
        "--empa-fixture",
        str(empa),
    ]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "no_version_change_detected"
    assert report["constraints"] == {
        "active_installation_unchanged": True,
        "deployed": False,
        "downloaded_candidate": False,
        "redistributed_external_database": False,
    }

    usgs.write_text("official page layout changed", encoding="utf-8")
    failed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert failed.returncode == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "parser_or_official_page_failure_requires_manual_review"
