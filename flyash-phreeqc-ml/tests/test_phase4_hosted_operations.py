"""Focused hosted-operation and backup/restore boundary tests."""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[1]


def _fake_docker(tmp_path: Path) -> tuple[dict[str, str], Path]:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    log = tmp_path / "docker.log"
    docker = binary_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$WPI_DOCKER_LOG\"\n"
        "if [ -n \"${WPI_FAKE_FAIL_MATCH:-}\" ]; then\n"
        "  case \"$*\" in *\"$WPI_FAKE_FAIL_MATCH\"*) exit 9 ;; esac\n"
        "fi\n"
        "case \" $* \" in\n"
        "  *' -czf - '*) printf 'bounded-fake-backup' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    environment = dict(os.environ)
    environment["PATH"] = f"{binary_dir}{os.pathsep}{environment['PATH']}"
    environment["WPI_DOCKER_LOG"] = str(log)
    environment["WPI_COMPOSE_FILE"] = "docker-compose.server.yml"
    return environment, log


def _valid_backup(path: Path) -> None:
    data = b'{}\n'
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo("app/outputs/tenant_test/projects/prj_test.json")
        member.size = len(data)
        member.mode = 0o600
        archive.addfile(member, io.BytesIO(data))


def _write_checksum(path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_name(f"{path.name}.sha256").write_text(
        f"{digest}  {path.name}\n", encoding="ascii")


def test_posix_backup_honors_compose_selector_and_never_reuses_a_name(tmp_path: Path) -> None:
    environment, log = _fake_docker(tmp_path)
    destination = tmp_path / "backups"
    command = ["sh", "scripts/backup-local.sh", str(destination)]
    for _ in range(2):
        completed = subprocess.run(
            command, cwd=ROOT, env=environment, text=True,
            capture_output=True, check=False,
        )
        assert completed.returncode == 0, completed.stderr
    archives = sorted(destination.glob("*.tar.gz"))
    assert len(archives) == 2
    assert len({item.name for item in archives}) == 2
    assert all(item.with_name(f"{item.name}.sha256").is_file() for item in archives)
    calls = log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 6
    assert all("compose -f docker-compose.server.yml" in item for item in calls)
    for offset in (0, 3):
        assert "stop app" in calls[offset]
        assert "run --rm --no-deps" in calls[offset + 1]
        assert "up --detach app" in calls[offset + 2]


def test_posix_backup_failure_restarts_app_and_leaves_no_partial_archive(tmp_path: Path) -> None:
    environment, log = _fake_docker(tmp_path)
    environment["WPI_FAKE_FAIL_MATCH"] = "run --rm --no-deps"
    destination = tmp_path / "backups"
    completed = subprocess.run(
        ["sh", "scripts/backup-local.sh", str(destination)],
        cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
    )
    assert completed.returncode != 0
    calls = log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 3
    assert "stop app" in calls[0]
    assert "run --rm --no-deps" in calls[1]
    assert "up --detach app" in calls[2]
    assert list(destination.iterdir()) == []


def test_posix_restore_requires_checksum_before_contacting_docker(tmp_path: Path) -> None:
    environment, log = _fake_docker(tmp_path)
    archive = tmp_path / "backup.tar.gz"
    _valid_backup(archive)
    completed = subprocess.run(
        ["sh", "scripts/restore-local.sh", "--yes", str(archive)],
        cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
    )
    assert completed.returncode == 65
    assert "checksum sidecar is required" in completed.stderr
    assert not log.exists()


def test_posix_restore_rejects_unreadable_archive_before_stopping_app(tmp_path: Path) -> None:
    environment, log = _fake_docker(tmp_path)
    archive = tmp_path / "backup.tar.gz"
    archive.write_bytes(b"not a gzip archive")
    _write_checksum(archive)
    completed = subprocess.run(
        ["sh", "scripts/restore-local.sh", "--yes", str(archive)],
        cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
    )
    assert completed.returncode == 65
    assert "cannot be read safely" in completed.stderr
    assert not log.exists()


def test_posix_restore_honors_compose_selector_and_safe_tar_flags(tmp_path: Path) -> None:
    environment, log = _fake_docker(tmp_path)
    archive = tmp_path / "backup.tar.gz"
    _valid_backup(archive)
    _write_checksum(archive)
    completed = subprocess.run(
        ["sh", "scripts/restore-local.sh", "--yes", str(archive)],
        cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 4
    assert all("compose -f docker-compose.server.yml" in item for item in calls)
    assert "stop app" in calls[0]
    assert "--entrypoint sh" in calls[1]
    assert "--user 0:0" in calls[1]
    assert "--cap-add CHOWN" in calls[1]
    assert "-xdev -mindepth 1 -depth -delete" in calls[1]
    assert "--no-same-owner --no-same-permissions" in calls[2]
    assert "/restore/backup.tar.gz:ro" in calls[2]
    assert "chown -R 10001:10001" in calls[2]
    assert "up --detach app" in calls[3]


def test_server_role_and_cross_platform_helper_configuration_is_fail_closed() -> None:
    compose = (ROOT / "docker-compose.server.yml").read_text(encoding="utf-8")
    entrypoint = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")
    backup_ps = (ROOT / "scripts/backup-local.ps1").read_text(encoding="utf-8")
    restore_ps = (ROOT / "scripts/restore-local.ps1").read_text(encoding="utf-8")
    assert "VLAB_ALLOWED_SUBJECTS: ${VLAB_ALLOWED_SUBJECTS:-}" in compose
    assert "VLAB_ALLOWED_TENANTS: ${VLAB_ALLOWED_TENANTS:-}" in compose
    assert "has_allowlist_entry" in entrypoint
    assert "separate viewer/member/admin role allow-list" in entrypoint
    for source in (backup_ps, restore_ps):
        assert "$env:WPI_COMPOSE_FILE" in source
        assert "docker compose -f $ComposeFile" in source
    assert "Could not stop the app before backup" in backup_ps
    assert "Backup failed and the app could not be restarted" in backup_ps
    assert "checksum sidecar is required" in restore_ps
    assert "Could not stop the app before restore" in restore_ps
    assert "Could not clear the five exact durable roots" in restore_ps
    assert "--user 0:0" in restore_ps
    assert "--cap-add CHOWN" in restore_ps
    assert "chown -R 10001:10001" in restore_ps


def test_posix_operational_scripts_are_valid_shell() -> None:
    for path in (
        "docker-entrypoint.sh",
        "scripts/backup-local.sh",
        "scripts/restore-local.sh",
    ):
        completed = subprocess.run(
            ["sh", "-n", path], cwd=ROOT, text=True,
            capture_output=True, check=False,
        )
        assert completed.returncode == 0, f"{path}: {completed.stderr}"
