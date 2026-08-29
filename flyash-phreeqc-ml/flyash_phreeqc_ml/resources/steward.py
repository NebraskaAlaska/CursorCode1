"""Deterministic Runtime & Database Steward with explicit promotion and rollback gates."""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .catalog import CatalogError, CatalogStore, atomic_write_bytes, atomic_write_json
from .bootstrap import ReleaseBootstrapError, bootstrap_release_resources
from .database import ExternalDatabaseImporter, parse_database_bytes, parse_database_file
from .models import (
    Citation,
    CompatibilityStatus,
    EvidenceRecord,
    EvidenceStatus,
    Finding,
    FindingSeverity,
    ProposalStatus,
    RedistributionState,
    ResourceComparison,
    ResourceContractError,
    ResourceKind,
    ResourceManifest,
    RollbackState,
    TestStatus,
    TemperatureRange,
    UpdateProposal,
    proposal_id_for,
    make_installation_id,
    sha256_bytes,
    utc_now,
)
from .sources import (
    DEFAULT_MAX_DOWNLOAD_BYTES,
    DownloadFetcher,
    OfficialSourcePolicy,
    SourcePolicyError,
    download_official_bytes,
    hash_archive_members,
    inspect_archive,
    archive_database_members,
    read_archive_member,
)

_PROPOSAL_RE = re.compile(r"^proposal-[0-9a-f]{24}$")
_MUTATION_LOCK = threading.RLock()
_CONTENT_SCAN_MAX_BYTES = 2 * 1024 * 1024
_SECRET_OR_MALWARE_MARKERS = (
    b"-----BEGIN " + b"OPENSSH " + b"PRIVATE " + b"KEY-----",
    b"-----BEGIN " + b"RSA " + b"PRIVATE " + b"KEY-----",
    b"-----BEGIN " + b"EC " + b"PRIVATE " + b"KEY-----",
    b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE",
)
_SECRET_ASSIGNMENT_RE = re.compile(
    rb"(?i)(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*"
    rb"['\"]?[A-Za-z0-9_./+\-=]{16,}"
)
_PROJECT_INTEGRATION_PROGRAM = r"""
import os
from pathlib import Path

from flyash_phreeqc_ml.simulation import phreeqc_executor as executor
from flyash_phreeqc_ml.simulation import phreeqc_input_builder as builder
from flyash_phreeqc_ml.simulation import phreeqc_run_contract as contract
from flyash_phreeqc_ml.simulation import source_terms

input_text = Path(os.environ["WPI_STEWARD_INPUT"]).read_text(encoding="utf-8")
preview = builder.PhreeqcInputPreview(
    scenario_id="STEWARD-PROJECT-INTEGRATION",
    phreeqc_input_text=input_text,
    template_type=builder.TEMPLATE_NAOH,
    status=builder.STATUS_READY,
    includes_source_terms=True,
    source_term_mode=source_terms.MODE_GLOBAL,
    source_term_status=source_terms.STATUS_RELEASE_INCLUDED,
)
executable = os.environ["WPI_STEWARD_EXECUTABLE"]
database = os.environ["WPI_STEWARD_DATABASE"]
availability = executor.check_availability(exe=executable, database=database)
if not availability.can_run or availability.environment_identity is None:
    raise SystemExit("candidate is unavailable through the project executor")
reviewed = contract.review_preview(preview)
confirmed = contract.confirm_reviewed(reviewed, availability.environment_identity)
result = executor.execute_preview(
    preview,
    confirmation=confirmed,
    workdir=Path(os.environ["WPI_STEWARD_WORKSPACE"]),
    exe=executable,
    database=database,
)
if result.status != executor.STATUS_SUCCESS or not result.selected_output_path:
    raise SystemExit("candidate failed the project execution contract")
print(result.environment_identity_hash)
"""


class StewardError(RuntimeError):
    """A Steward command failed closed without changing the active resource."""


def compare_resource_manifests(current: ResourceManifest | None,
                               candidate: ResourceManifest) -> ResourceComparison:
    old = current.supported_summary if current else candidate.supported_summary.__class__()
    new = candidate.supported_summary

    def delta(before, after):
        return tuple(sorted(set(after) - set(before))), tuple(sorted(set(before) - set(after)))

    added_master, removed_master = delta(old.master_species, new.master_species)
    added_solution, removed_solution = delta(old.solution_species, new.solution_species)
    added_phases, removed_phases = delta(old.phases, new.phases)
    warnings = set(candidate.warnings)
    if removed_master:
        warnings.add("candidate removes parsed master species")
    if removed_solution:
        warnings.add("candidate removes parsed solution species")
    if removed_phases:
        warnings.add("candidate removes parsed phases")
    return ResourceComparison(
        current_installation_id=current.installation_id if current else "",
        candidate_installation_id=candidate.installation_id,
        current_version=current.installed_version if current else "",
        candidate_version=candidate.installed_version,
        current_sha256=current.primary_sha256 if current else "",
        candidate_sha256=candidate.primary_sha256,
        added_master_species=added_master,
        removed_master_species=removed_master,
        added_solution_species=added_solution,
        removed_solution_species=removed_solution,
        added_phases=added_phases,
        removed_phases=removed_phases,
        warnings=tuple(sorted(warnings)),
    )


def _run_worker(command: list[str], *, cwd: Path, timeout: float,
                extra_env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Run one fixed Steward worker command with bounded, non-secret diagnostics."""
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env={
                **os.environ,
                "LC_ALL": "C",
                "LANG": "C",
                **({str(key): str(value) for key, value in extra_env.items()}
                   if extra_env else {}),
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StewardError(
            f"candidate worker failed to complete ({type(exc).__name__})") from exc
    stdout = completed.stdout[-32_768:]
    stderr = completed.stderr[-32_768:]
    if completed.returncode != 0:
        raise StewardError(
            f"candidate worker exited {completed.returncode}: "
            f"{(stderr or stdout or 'no diagnostic')[-2_000:]}")
    return stdout, stderr


def _provider_for_url(url: str) -> str:
    host = str(url).split("/", 3)[2].lower() if "://" in str(url) else ""
    if host.endswith("usgs.gov"):
        return "U.S. Geological Survey"
    if host.endswith("empa.ch"):
        return "Empa"
    return "Allowlisted official provider"


class ResourceSteward:
    """State-machine facade for update discovery through explicit rollback."""

    def __init__(self, store: CatalogStore, *, policy: OfficialSourcePolicy | None = None,
                 max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES):
        self.store = store
        self.policy = policy or OfficialSourcePolicy()
        self.max_download_bytes = max_download_bytes

    def _proposal_path(self, proposal_id: str) -> Path:
        if not _PROPOSAL_RE.fullmatch(str(proposal_id)):
            raise StewardError("invalid proposal identity")
        return self.store.proposals_dir / f"{proposal_id}.json"

    def _save(self, proposal: UpdateProposal, *, create_only: bool = False) -> UpdateProposal:
        with _MUTATION_LOCK:
            self.store.proposals_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self._proposal_path(proposal.proposal_id), proposal.to_dict(),
                              create_only=create_only)
        return proposal

    def show(self, proposal_id: str | None = None):
        if proposal_id is None:
            if not self.store.proposals_dir.exists():
                return ()
            proposals = []
            for path in sorted(self.store.proposals_dir.glob("proposal-*.json")):
                if path.is_symlink():
                    raise StewardError("proposal document must not be a symlink")
                proposals.append(self._load_path(path))
            return tuple(proposals)
        return self._load_path(self._proposal_path(proposal_id))

    def _load_path(self, path: Path) -> UpdateProposal:
        if path.is_symlink() or not path.is_file():
            raise StewardError(f"proposal not found: {path.stem}")
        try:
            return UpdateProposal.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ResourceContractError) as exc:
            raise StewardError(f"cannot read proposal evidence: {exc}") from exc

    def check(self, *, resource_id: str, resource_kind: ResourceKind,
              candidate_version: str, source_url: str, archive_filename: str,
              expected_source_sha256: str = "", rights_notice: str = "",
              redistribution_state: RedistributionState = RedistributionState.UNKNOWN) \
            -> UpdateProposal:
        """Create a deterministic proposal from official metadata supplied by a discoverer."""
        try:
            self.policy.validate(source_url)
            kind = ResourceKind(resource_kind)
            redistribution = RedistributionState(redistribution_state)
        except (SourcePolicyError, TypeError, ValueError) as exc:
            raise StewardError(str(exc)) from exc
        proposal_id = proposal_id_for(
            resource_id, candidate_version, source_url, archive_filename)
        current = self.store.get_active(resource_id)
        proposal = UpdateProposal(
            proposal_id=proposal_id,
            resource_id=resource_id,
            resource_kind=kind,
            candidate_version=candidate_version,
            source_url=source_url,
            archive_filename=archive_filename,
            current_installation_id=current.installation_id if current else "",
            previous_active_installation_id=current.installation_id if current else "",
            expected_source_sha256=expected_source_sha256,
            rights_notice=rights_notice,
            redistribution_state=redistribution,
        )
        path = self._proposal_path(proposal_id)
        if path.exists():
            existing = self.show(proposal_id)
            immutable = (
                existing.resource_id, existing.resource_kind, existing.candidate_version,
                existing.source_url, existing.archive_filename, existing.expected_source_sha256,
            )
            requested = (
                proposal.resource_id, proposal.resource_kind, proposal.candidate_version,
                proposal.source_url, proposal.archive_filename, proposal.expected_source_sha256,
            )
            if immutable != requested:
                raise StewardError("proposal identity already exists with different metadata")
            return existing
        return self._save(proposal, create_only=True)

    def download_candidate(self, proposal_id: str, *, fetcher: DownloadFetcher | None = None) \
            -> UpdateProposal:
        proposal = self.show(proposal_id)
        if proposal.status != ProposalStatus.PROPOSED:
            raise StewardError("download-candidate requires a proposed update")
        try:
            self.policy.validate(proposal.source_url)
            raw = (fetcher(proposal.source_url) if fetcher else download_official_bytes(
                proposal.source_url, policy=self.policy, max_bytes=self.max_download_bytes))
        except (SourcePolicyError, OSError) as exc:
            raise StewardError(str(exc)) from exc
        if not isinstance(raw, bytes) or not raw or len(raw) > self.max_download_bytes:
            raise StewardError("candidate download is empty, non-bytes, or exceeds the size cap")
        directory = self.store.quarantine_dir / proposal.proposal_id
        if directory.exists() and directory.is_symlink():
            raise StewardError("candidate quarantine directory must not be a symlink")
        directory.mkdir(parents=True, exist_ok=True)
        archive = directory / proposal.archive_filename
        digest = sha256_bytes(raw)
        if archive.exists():
            if archive.is_symlink() or sha256_bytes(archive.read_bytes()) != digest:
                raise StewardError("quarantine already contains different candidate bytes")
        else:
            atomic_write_bytes(archive, raw, create_only=True)
        return self._save(replace(
            proposal,
            status=ProposalStatus.DOWNLOADED,
            updated_at=utc_now(),
            observed_source_sha256=digest,
            quarantine_path=str(archive.resolve()),
        ))

    def _reject(self, proposal: UpdateProposal, finding_id: str, summary: str) -> None:
        finding = Finding(finding_id, FindingSeverity.HIGH, summary)
        self._save(replace(
            proposal,
            status=ProposalStatus.REJECTED,
            updated_at=utc_now(),
            findings=(*proposal.findings, finding),
        ))
        raise StewardError(summary)

    @staticmethod
    def _candidate_path(proposal: UpdateProposal) -> Path:
        path = Path(proposal.quarantine_path)
        try:
            details = path.lstat()
        except OSError as exc:
            raise StewardError(f"candidate is missing from quarantine: {exc}") from exc
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise StewardError("candidate must remain a regular non-symlink quarantine file")
        return path.resolve(strict=True)

    def verify_candidate(self, proposal_id: str, *, rights_notice: str | None = None,
                         redistribution_state: RedistributionState | None = None) \
            -> UpdateProposal:
        proposal = self.show(proposal_id)
        if proposal.status != ProposalStatus.DOWNLOADED:
            raise StewardError("verify-candidate requires a quarantined download")
        path = self._candidate_path(proposal)
        digest = sha256_bytes(path.read_bytes())
        if digest != proposal.observed_source_sha256:
            self._reject(proposal, "candidate-bytes-changed",
                         "candidate bytes changed after quarantine download")
        if proposal.expected_source_sha256 and digest != proposal.expected_source_sha256:
            self._reject(proposal, "candidate-hash-mismatch",
                         "candidate SHA-256 does not match the exact proposal hash")
        notice = proposal.rights_notice if rights_notice is None else str(rights_notice).strip()
        redistribution = proposal.redistribution_state if redistribution_state is None \
            else RedistributionState(redistribution_state)
        if not notice:
            self._reject(proposal, "candidate-rights-missing",
                         "candidate rights/licence evidence is missing")
        if redistribution == RedistributionState.PROHIBITED:
            self._reject(proposal, "candidate-redistribution-prohibited",
                         "candidate is marked prohibited and cannot be installed")
        try:
            self.policy.validate(proposal.source_url)
            if path.suffix.lower() == ".dat":
                parse_database_file(path, max_bytes=self.max_download_bytes)
                members: tuple = ()
                candidate_file_hashes = {path.name: digest}
            else:
                members = inspect_archive(path)
                candidate_file_hashes = hash_archive_members(path)
        except (SourcePolicyError, ValueError) as exc:
            self._reject(proposal, "candidate-safety-verification-failed", str(exc))
            raise AssertionError("unreachable")

        suspicious = (
            ".env", "secrets.toml", "id_rsa", "id_ed25519", "credentials", ".pem", ".key",
        )
        findings = list(proposal.findings)
        for member in members:
            lower = member.name.lower()
            if any(token in lower for token in suspicious):
                self._reject(
                    proposal,
                    "suspicious-member-" + sha256_bytes(member.name.encode())[:16],
                    "candidate archive contains a secret-like path and remains quarantined",
                )

        # This is deliberately a basic, bounded scanner rather than a claim of malware
        # certification.  It never emits candidate content or a detected credential.
        scan_targets: list[tuple[str, bytes]] = []
        if path.suffix.lower() == ".dat":
            scan_targets.append((path.name, path.read_bytes()))
        else:
            for member in members:
                if member.is_directory or member.size_bytes > _CONTENT_SCAN_MAX_BYTES:
                    continue
                try:
                    scan_targets.append((
                        member.name,
                        read_archive_member(
                            path, member.name, max_bytes=_CONTENT_SCAN_MAX_BYTES),
                    ))
                except SourcePolicyError as exc:
                    self._reject(
                        proposal,
                        "candidate-content-scan-failed",
                        f"candidate content safety scan failed closed: {exc}",
                    )
        for member_name, raw in scan_targets:
            if any(marker in raw for marker in _SECRET_OR_MALWARE_MARKERS) \
                    or _SECRET_ASSIGNMENT_RE.search(raw):
                self._reject(
                    proposal,
                    "candidate-secret-or-malware-marker-"
                    + sha256_bytes(member_name.encode())[:16],
                    "candidate content matched a secret/malware safety marker and remains quarantined",
                )
        return self._save(replace(
            proposal,
            status=ProposalStatus.VERIFIED,
            updated_at=utc_now(),
            observed_source_sha256=digest,
            candidate_file_hashes=candidate_file_hashes,
            rights_notice=notice,
            redistribution_state=redistribution,
            findings=tuple(findings),
        ))

    def _build_database_candidate(
        self,
        proposal: UpdateProposal,
        source: Path,
        *,
        archive_member: str | None = None,
    ) -> tuple[ResourceManifest, tuple[EvidenceRecord, ...]]:
        manifest = ExternalDatabaseImporter(
            self.store, max_bytes=self.max_download_bytes).import_database(
                source,
                resource_id=proposal.resource_id,
                display_name=f"{_provider_for_url(proposal.source_url)} database candidate",
                provider=_provider_for_url(proposal.source_url),
                installed_version=proposal.candidate_version,
                rights_confirmed=True,
                rights_notice=proposal.rights_notice,
                redistribution_state=proposal.redistribution_state,
                actor_role="admin",
                actor_type="human",
                deployment_scope="hosted",
                archive_member=archive_member,
                official_source_url=proposal.source_url,
                citation=Citation(
                    title=f"Official database candidate {proposal.candidate_version}",
                    authors=(_provider_for_url(proposal.source_url),),
                    url=proposal.source_url,
                    source_location=proposal.archive_filename,
                    extraction_confidence=1.0,
                ),
                domain_notes=(
                    "Candidate installed side by side; scientific suitability requires review.",
                ),
                resource_kind=proposal.resource_kind,
            )
        return manifest, (EvidenceRecord(
            "builtin-database-parse-install",
            EvidenceStatus.PASSED,
            "Steward parsed and installed one exact database member without concatenation.",
            manifest.primary_sha256,
        ),)

    def _extract_runtime_source(self, archive: Path, destination: Path) -> Path:
        inventory = inspect_archive(archive)
        destination.mkdir(parents=True, exist_ok=False)
        try:
            if zipfile.is_zipfile(archive):
                with zipfile.ZipFile(archive) as handle:
                    handle.extractall(destination)
            elif tarfile.is_tarfile(archive):
                with tarfile.open(archive, mode="r:*") as handle:
                    # Python's data filter rejects links, devices, absolute paths, and traversal;
                    # the independently validated inventory above already enforces the same closed
                    # contract before any filesystem write.
                    handle.extractall(destination, filter="data")
            else:
                raise StewardError("runtime candidate is not a supported ZIP/TAR archive")
        except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
            raise StewardError("runtime candidate extraction failed safely") from exc
        extracted_files = sum(1 for path in destination.rglob("*") if path.is_file())
        expected_files = sum(1 for item in inventory if not item.is_directory)
        if extracted_files != expected_files:
            raise StewardError("runtime candidate extraction is incomplete")
        configure = [
            path for path in destination.rglob("configure")
            if path.is_file() and (path.parent / "src").is_dir()
            and (path.parent / "database").is_dir()
        ]
        if len(configure) != 1:
            raise StewardError("runtime archive does not contain one unambiguous source root")
        for name in ("configure", "config.guess", "config.sub", "install-sh", "missing", "depcomp"):
            for path in configure[0].parent.rglob(name):
                if path.is_file() and not path.is_symlink():
                    path.chmod(0o755)
        return configure[0].parent

    def _build_runtime_candidate(
        self,
        proposal: UpdateProposal,
        source: Path,
    ) -> tuple[ResourceManifest, tuple[EvidenceRecord, ...]]:
        build_parent = self.store.quarantine_dir / proposal.proposal_id
        build_root = Path(tempfile.mkdtemp(prefix="trusted-build-", dir=build_parent))
        source_root = self._extract_runtime_source(source, build_root / "source")
        _run_worker(
            ["sh", "./configure", "--prefix=/opt/phreeqc-candidate"],
            cwd=source_root,
            timeout=300,
        )
        jobs = max(1, min(int(os.cpu_count() or 1), 8))
        _run_worker(["make", f"-j{jobs}"], cwd=source_root, timeout=1_200)
        check_stdout, _ = _run_worker(["make", "check"], cwd=source_root, timeout=1_200)
        summary = re.search(r"(?m)^# PASS:\s+([1-9][0-9]*)\s*$", check_stdout)
        if not summary or not re.search(r"(?m)^# FAIL:\s+0\s*$", check_stdout) \
                or not re.search(r"(?m)^# ERROR:\s+0\s*$", check_stdout):
            raise StewardError("official source test suite did not report a complete pass")
        built = source_root / "src" / "phreeqc"
        if not built.is_file() or built.is_symlink():
            raise StewardError("trusted runtime build did not produce src/phreeqc")
        executable_raw = built.read_bytes()
        executable_hash = sha256_bytes(executable_raw)
        installation_id = make_installation_id(
            proposal.resource_id, proposal.candidate_version, executable_hash)
        install_root = self.store.installation_dir(installation_id)
        if install_root.exists() and install_root.is_symlink():
            raise StewardError("runtime installation directory must not be a symlink")
        (install_root / "bin").mkdir(parents=True, exist_ok=True)
        (install_root / "database").mkdir(parents=True, exist_ok=True)
        (install_root / "examples").mkdir(parents=True, exist_ok=True)
        executable = install_root / "bin" / "phreeqc"
        if executable.exists():
            if executable.is_symlink() or sha256_bytes(executable.read_bytes()) != executable_hash:
                raise StewardError("runtime installation path contains different executable bytes")
        else:
            atomic_write_bytes(executable, executable_raw, create_only=True)
        executable.chmod(0o755)
        for path in sorted((source_root / "database").glob("*.dat")):
            atomic_write_bytes(
                install_root / "database" / path.name, path.read_bytes(), create_only=True)
        for path in sorted((source_root / "examples").iterdir()):
            if path.is_file() and not path.is_symlink():
                atomic_write_bytes(
                    install_root / "examples" / path.name, path.read_bytes(), create_only=True)
        manifest = ResourceManifest(
            resource_id=proposal.resource_id,
            installation_id=installation_id,
            resource_kind=ResourceKind.PHREEQC_RUNTIME,
            display_name=f"Official PHREEQC {proposal.candidate_version} candidate runtime",
            provider=_provider_for_url(proposal.source_url),
            official_source_url=proposal.source_url,
            discovered_version=proposal.candidate_version,
            installed_version=proposal.candidate_version,
            archive_filename=proposal.archive_filename,
            source_sha256=proposal.observed_source_sha256,
            executable_sha256=executable_hash,
            content_sha256=executable_hash,
            architectures=(platform.machine().lower(),),
            operating_system_targets=(sys.platform,),
            build_toolchain=("configure", "make"),
            install_path=str(executable.resolve()),
            installed_at=utc_now(),
            verified_at=utc_now(),
            citation=Citation(
                title=f"Official PHREEQC {proposal.candidate_version} source distribution",
                authors=(_provider_for_url(proposal.source_url),),
                url=proposal.source_url,
                source_location=proposal.archive_filename,
                extraction_confidence=1.0,
            ),
            rights_notice=proposal.rights_notice,
            redistribution_state=proposal.redistribution_state,
            test_status=TestStatus.NOT_TESTED,
            standalone_test_status=TestStatus.NOT_TESTED,
            warnings=(
                "Candidate is side-by-side and inactive until deterministic tests and explicit promotion.",
            ),
        )
        atomic_write_json(
            install_root / "resource_manifest.json", manifest.to_dict(), create_only=True)
        return manifest, (
            EvidenceRecord(
                "builtin-configure-make",
                EvidenceStatus.PASSED,
                "Steward extracted the reviewed archive and completed configure/make locally.",
                executable_hash,
            ),
            EvidenceRecord(
                "builtin-official-make-check",
                EvidenceStatus.PASSED,
                f"Official source test suite completed {summary.group(1)} passing examples/tests.",
                executable_hash,
            ),
        )

    def builtin_builder(
        self, *, archive_member: str | None = None,
    ) -> Callable[[UpdateProposal, Path], tuple[ResourceManifest, Iterable[EvidenceRecord]]]:
        """Return the closed, code-owned worker used by the CLI; no JSON can self-attest."""
        def worker(proposal: UpdateProposal, source: Path):
            if proposal.resource_kind == ResourceKind.PHREEQC_RUNTIME:
                if archive_member:
                    raise StewardError("archive_member is not valid for a runtime source build")
                return self._build_runtime_candidate(proposal, source)
            if proposal.resource_kind in {
                ResourceKind.PHREEQC_OFFICIAL_DATABASE,
                ResourceKind.EXTERNAL_THERMODYNAMIC_DATABASE,
                ResourceKind.DATABASE_EXTENSION,
            }:
                return self._build_database_candidate(
                    proposal, source, archive_member=archive_member)
            raise StewardError("no built-in candidate builder exists for this resource kind")
        return worker

    @staticmethod
    def _assert_phreeqc_success(executable: Path, database: Path, input_text: str) -> str:
        with tempfile.TemporaryDirectory(prefix="wpi-steward-test-") as temporary:
            root = Path(temporary)
            input_path = root / "candidate.pqi"
            output_path = root / "candidate.pqo"
            input_path.write_text(input_text, encoding="utf-8")
            _run_worker(
                [str(executable), str(input_path), str(output_path), str(database)],
                cwd=root,
                timeout=120,
            )
            if not output_path.is_file():
                raise StewardError("candidate PHREEQC test did not create an output file")
            output = output_path.read_text(encoding="utf-8", errors="replace")
            if "End of Run" not in output or re.search(r"\bERROR\b", output):
                raise StewardError("candidate PHREEQC output failed its completion/error gate")
            selected = root / "selected.out"
            if not selected.is_file() or not selected.read_text(
                    encoding="utf-8", errors="replace").strip():
                raise StewardError("candidate PHREEQC test did not create selected output")
            return sha256_bytes(output_path.read_bytes())

    @staticmethod
    def _project_integration_test(executable: Path, database: Path, input_text: str) -> str:
        """Exercise the candidate through the application's review/confirmation/executor path."""
        package_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix="wpi-steward-project-") as temporary:
            root = Path(temporary)
            input_path = root / "candidate-project-integration.pqi"
            input_path.write_text(input_text, encoding="utf-8")
            stdout, _ = _run_worker(
                [sys.executable, "-c", _PROJECT_INTEGRATION_PROGRAM],
                cwd=package_root,
                timeout=180,
                extra_env={
                    "WPI_STEWARD_INPUT": str(input_path),
                    "WPI_STEWARD_EXECUTABLE": str(executable),
                    "WPI_STEWARD_DATABASE": str(database),
                    "WPI_STEWARD_WORKSPACE": str(root / "jobs"),
                    # A candidate is intentionally inactive, so a live release catalog must
                    # not be allowed to rebind this fixed integration test to active resources.
                    "VLAB_RESOURCE_CATALOG": "",
                    "VLAB_RESOURCE_ROOT": "",
                    "VLAB_ACTIVE_RUNTIME_INSTALLATION_ID": "",
                    "VLAB_ACTIVE_DATABASE_INSTALLATION_ID": "",
                    "VLAB_RESOURCE_BOOTSTRAP_RESULT": "",
                    "VLAB_KNOWLEDGE_PACK_HASH": "",
                    # Release-image identity declarations describe the active bundled runtime,
                    # not this quarantined candidate. Leaving one behind can correctly make the
                    # executor reject candidate paths as a manifest mismatch. The Steward proves
                    # the candidate's exact file identities directly in this isolated process.
                    "PHREEQC_RUNTIME_MANIFEST": "",
                    "PHREEQC_RUNTIME_MANIFEST_ID": "",
                    "PHREEQC_RUNTIME_ID": "",
                    "PHREEQC_SOURCE_MANIFEST": "",
                    "PHREEQC_SOURCE_MANIFEST_SHA256": "",
                    "PHREEQC_SOURCE_ID": "",
                    "PHREEQC_DATABASE_ID": "",
                    "PHREEQC_DATABASE_MANIFEST_ID": "",
                    "PHREEQC_DATABASE_SHA256": "",
                    "PHREEQC_DATABASE_VERSION": "",
                    "PHREEQC_CONTAINER_IMAGE_DIGEST": "",
                    "PHREEQC_VERSION": "",
                    "PHREEQC_EXE": str(executable),
                    "PHREEQC_DATABASE": str(database),
                },
            )
            identity = stdout.strip().splitlines()[-1] if stdout.strip() else ""
            if not re.fullmatch(r"[0-9a-f]{64}", identity):
                raise StewardError("project integration test omitted its environment identity")
            return identity

    def builtin_tester(
        self,
    ) -> Callable[[ResourceManifest], tuple[Iterable[EvidenceRecord], Iterable[Finding]]]:
        """Return actual executable/database smoke tests owned by deterministic Steward code."""
        def worker(manifest: ResourceManifest):
            minimal = (
                "TITLE Steward candidate software smoke; not experimental validation\n"
                "SOLUTION 1\n    temp 25\n    pH 7\n"
                "SELECTED_OUTPUT\n    -file selected.out\n    -reset false\n    -pH true\nEND\n"
            )
            if manifest.resource_kind == ResourceKind.PHREEQC_RUNTIME:
                executable = Path(manifest.install_path)
                install_root = executable.parent.parent
                database = install_root / "database" / "phreeqc.dat"
                artifact = self._assert_phreeqc_success(executable, database, minimal)
                evidence = (
                    EvidenceRecord(
                        "builtin-runtime-minimal-selected-output",
                        EvidenceStatus.PASSED,
                        "Candidate runtime completed a minimal reviewed software smoke.",
                        artifact,
                    ),
                )
                example = install_root / "examples" / "ex1"
                if example.is_file() and not example.is_symlink():
                    with tempfile.TemporaryDirectory(prefix="wpi-steward-example-") as temporary:
                        output = Path(temporary) / "official-example.pqo"
                        _run_worker(
                            [str(executable), str(example), str(output), str(database)],
                            cwd=Path(temporary), timeout=120)
                        text = output.read_text(encoding="utf-8", errors="replace")
                        if "End of Run" not in text or re.search(r"\bERROR\b", text):
                            raise StewardError("candidate runtime official example failed")
                        evidence = (*evidence, EvidenceRecord(
                            "builtin-official-example",
                            EvidenceStatus.PASSED,
                            "Candidate runtime completed its bundled official example.",
                            sha256_bytes(output.read_bytes()),
                        ))
                integration_identity = self._project_integration_test(
                    executable, database, minimal)
                evidence = (*evidence, EvidenceRecord(
                    "builtin-project-executor-integration",
                    EvidenceStatus.PASSED,
                    "Candidate completed the project review, confirmation, and executor contract.",
                    integration_identity,
                ))
                return evidence, ()

            state = self.store.load()
            runtimes = [
                item for item in state.resources
                if item.resource_kind == ResourceKind.PHREEQC_RUNTIME
                and item.installation_id in set(state.active.values())
            ]
            if len(runtimes) != 1:
                raise StewardError("database candidate test requires one active runtime")
            executable = Path(runtimes[0].install_path)
            database = Path(manifest.install_path)
            input_text = minimal
            if manifest.resource_kind == ResourceKind.DATABASE_EXTENSION:
                bases = [
                    item for item in state.resources
                    if item.resource_kind in {
                        ResourceKind.PHREEQC_OFFICIAL_DATABASE,
                        ResourceKind.EXTERNAL_THERMODYNAMIC_DATABASE,
                    }
                    and item.database_family == manifest.requires_database_family
                ]
                if not bases:
                    raise StewardError("database extension test cannot resolve its declared base")
                database = Path(bases[0].install_path)
                input_text = f'INCLUDE$ "{Path(manifest.install_path).resolve()}"\n' + minimal
            artifact = self._assert_phreeqc_success(executable, database, input_text)
            integration_identity = self._project_integration_test(
                executable, database, input_text)
            return (
                EvidenceRecord(
                    "builtin-database-load-selected-output",
                    EvidenceStatus.PASSED,
                    "Active runtime loaded the exact candidate database in a minimal software smoke.",
                    artifact,
                ),
                EvidenceRecord(
                    "builtin-project-executor-integration",
                    EvidenceStatus.PASSED,
                    "Candidate database completed the project review, confirmation, and executor contract.",
                    integration_identity,
                ),
            ), ()
        return worker

    def build_candidate(
        self,
        proposal_id: str,
        *,
        archive_member: str | None = None,
    ) -> UpdateProposal:
        """Build using only the Steward-owned deterministic implementation.

        Callers may select one already-inventoried database member, but cannot submit a
        manifest, evidence document, command, or callback that can attest its own success.
        """
        proposal = self.show(proposal_id)
        if proposal.status != ProposalStatus.VERIFIED:
            raise StewardError("build-candidate requires a verified candidate")
        try:
            candidate_path = self._candidate_path(proposal)
            if sha256_bytes(candidate_path.read_bytes()) != proposal.observed_source_sha256:
                self._reject(
                    proposal,
                    "candidate-bytes-changed-before-build",
                    "candidate bytes changed after verification and remain quarantined",
                )
            manifest, evidence = self.builtin_builder(archive_member=archive_member)(
                proposal, candidate_path)
        except Exception as exc:  # noqa: BLE001 - build failures become evidence, never promotion
            raise StewardError(f"candidate build failed: {type(exc).__name__}: {exc}") from exc
        if not isinstance(manifest, ResourceManifest):
            raise StewardError("candidate build must return a validated ResourceManifest")
        evidence = tuple(evidence)
        if not evidence or any(not isinstance(item, EvidenceRecord) for item in evidence):
            raise StewardError("candidate build requires structured build evidence")
        if any(item.status != EvidenceStatus.PASSED for item in evidence):
            raise StewardError("candidate build evidence is incomplete or failed")
        if manifest.resource_id != proposal.resource_id \
                or manifest.resource_kind != proposal.resource_kind \
                or manifest.installed_version != proposal.candidate_version:
            raise StewardError("built manifest does not match the exact update proposal")
        if manifest.source_sha256 != proposal.observed_source_sha256:
            raise StewardError("built manifest is not bound to the quarantined source hash")
        if not manifest.primary_sha256:
            raise StewardError("built candidate has no executable/database/content hash")
        if manifest.rollback_state != RollbackState.CANDIDATE:
            raise StewardError("built candidate must remain non-active")
        if manifest.install_path:
            install_path = Path(manifest.install_path)
            if not install_path.exists() or install_path.is_symlink():
                raise StewardError("built candidate install path is unavailable or a symlink")
        try:
            self.store.register(manifest)
        except CatalogError as exc:
            raise StewardError(str(exc)) from exc
        return self._save(replace(
            proposal,
            status=ProposalStatus.BUILT,
            updated_at=utc_now(),
            candidate_installation_id=manifest.installation_id,
            candidate_sha256=manifest.primary_sha256,
            build_evidence=evidence,
        ))

    def test_candidate(
        self,
        proposal_id: str,
    ) -> UpdateProposal:
        """Execute only the Steward-owned smoke/official-example test implementation."""
        proposal = self.show(proposal_id)
        if proposal.status != ProposalStatus.BUILT:
            raise StewardError("test-candidate requires a built candidate")
        manifest = self.store.get_installation(proposal.candidate_installation_id)
        try:
            evidence, findings = self.builtin_tester()(manifest)
        except Exception as exc:  # noqa: BLE001
            raise StewardError(f"candidate tests failed to run: {type(exc).__name__}: {exc}") from exc
        evidence = tuple(evidence)
        findings = tuple(findings)
        if not evidence or any(not isinstance(item, EvidenceRecord) for item in evidence):
            raise StewardError("candidate test evidence is required")
        if any(not isinstance(item, Finding) for item in findings):
            raise StewardError("candidate findings must use the closed Finding contract")
        if any(item.status != EvidenceStatus.PASSED for item in evidence):
            failure = Finding(
                finding_id="candidate-tests-not-complete",
                severity=FindingSeverity.HIGH,
                summary="candidate tests contain a failed or skipped required check",
            )
            self._save(replace(
                proposal,
                updated_at=utc_now(),
                test_evidence=evidence,
                findings=(*proposal.findings, *findings, failure),
            ))
            raise StewardError(failure.summary)
        if manifest.resource_kind == ResourceKind.DATABASE_EXTENSION:
            tested_manifest = replace(
                manifest,
                test_status=TestStatus.NOT_APPLICABLE_REQUIRES_BASE,
                standalone_test_status=TestStatus.NOT_APPLICABLE_REQUIRES_BASE,
                base_include_test_status=TestStatus.PASSED,
                compatibility_status=CompatibilityStatus.COMPATIBLE,
            )
        else:
            tested_manifest = replace(
                manifest,
                test_status=TestStatus.PASSED,
                standalone_test_status=TestStatus.PASSED,
            )
        self.store.replace_manifest(tested_manifest)
        return self._save(replace(
            proposal,
            status=ProposalStatus.TESTED,
            updated_at=utc_now(),
            test_evidence=evidence,
            findings=(*proposal.findings, *findings),
        ))

    def compare(self, proposal_id: str) -> ResourceComparison:
        proposal = self.show(proposal_id)
        if proposal.status not in {ProposalStatus.BUILT, ProposalStatus.TESTED}:
            raise StewardError("compare requires a built or tested candidate")
        candidate = self.store.get_installation(proposal.candidate_installation_id)
        current = (self.store.get_installation(proposal.current_installation_id)
                   if proposal.current_installation_id else None)
        comparison = compare_resource_manifests(current, candidate)
        self._save(replace(proposal, comparison=comparison, updated_at=utc_now()))
        return comparison

    def resolve_finding(self, proposal_id: str, finding_id: str, *, resolution: str,
                        actor_role: str, actor_type: str = "human") -> UpdateProposal:
        if actor_type != "human" or actor_role != "admin" or not resolution.strip():
            raise StewardError("finding resolution requires an explicit human administrator reason")
        proposal = self.show(proposal_id)
        found = False
        findings = []
        for item in proposal.findings:
            if item.finding_id == finding_id:
                found = True
                findings.append(replace(item, resolved=True, resolution=resolution.strip()))
            else:
                findings.append(item)
        if not found:
            raise StewardError(f"unknown finding: {finding_id}")
        return self._save(replace(proposal, findings=tuple(findings), updated_at=utc_now()))

    def promote(self, proposal_id: str, *, candidate_sha256: str, admin_confirmation: str,
                actor_role: str, actor_type: str = "human") -> UpdateProposal:
        proposal = self.show(proposal_id)
        expected_confirmation = f"PROMOTE {proposal.proposal_id} {proposal.candidate_sha256}"
        if actor_type != "human" or actor_role != "admin" \
                or admin_confirmation != expected_confirmation:
            raise StewardError("promotion requires the exact human administrator confirmation")
        if proposal.status != ProposalStatus.TESTED:
            raise StewardError("only a completely tested candidate may be promoted")
        if candidate_sha256 != proposal.candidate_sha256:
            raise StewardError("promotion candidate hash does not match the exact proposal")
        if not proposal.test_evidence \
                or any(item.status != EvidenceStatus.PASSED for item in proposal.test_evidence):
            raise StewardError("promotion is blocked until every required test passes")
        if proposal.unresolved_blockers:
            raise StewardError("promotion is blocked by unresolved high/medium findings")
        candidate = self.store.get_installation(proposal.candidate_installation_id)
        extension_tested = (
            candidate.resource_kind == ResourceKind.DATABASE_EXTENSION
            and candidate.test_status == TestStatus.NOT_APPLICABLE_REQUIRES_BASE
            and candidate.standalone_test_status == TestStatus.NOT_APPLICABLE_REQUIRES_BASE
            and candidate.base_include_test_status == TestStatus.PASSED
            and candidate.compatibility_status == CompatibilityStatus.COMPATIBLE
        )
        if candidate.primary_sha256 != candidate_sha256 or not (
                candidate.test_status == TestStatus.PASSED or extension_tested):
            raise StewardError("catalog candidate identity/test status differs from proposal evidence")
        live = self.store.get_active(proposal.resource_id)
        live_id = live.installation_id if live else ""
        if live_id != proposal.current_installation_id:
            raise StewardError("active resource changed after proposal creation; compare again")
        try:
            self.store.activate(
                proposal.resource_id, proposal.candidate_installation_id,
                proposal_id=proposal.proposal_id)
        except CatalogError as exc:
            raise StewardError(str(exc)) from exc
        return self._save(replace(
            proposal,
            status=ProposalStatus.PROMOTED,
            updated_at=utc_now(),
            previous_active_installation_id=live_id,
        ))

    def rollback(self, proposal_id: str, *, admin_confirmation: str,
                 actor_role: str, actor_type: str = "human") -> UpdateProposal:
        proposal = self.show(proposal_id)
        expected = f"ROLLBACK {proposal.proposal_id} {proposal.candidate_sha256}"
        if actor_type != "human" or actor_role != "admin" or admin_confirmation != expected:
            raise StewardError("rollback requires the exact human administrator confirmation")
        if proposal.status != ProposalStatus.PROMOTED:
            raise StewardError("rollback requires a previously promoted proposal")
        if not proposal.previous_active_installation_id:
            raise StewardError("proposal has no prior active installation to restore")
        live = self.store.get_active(proposal.resource_id)
        if live is None or live.installation_id != proposal.candidate_installation_id:
            raise StewardError("active resource no longer matches the promoted candidate")
        try:
            self.store.rollback_to(
                proposal.resource_id, proposal.previous_active_installation_id,
                proposal_id=proposal.proposal_id)
        except CatalogError as exc:
            raise StewardError(str(exc)) from exc
        return self._save(replace(
            proposal, status=ProposalStatus.ROLLED_BACK, updated_at=utc_now()))

    def export_report(self, proposal_id: str, output_dir: str | Path) -> tuple[Path, Path]:
        proposal = self.show(proposal_id)
        destination = Path(output_dir).expanduser().absolute()
        if destination.exists() and destination.is_symlink():
            raise StewardError("report destination must not be a symlink")
        destination.mkdir(parents=True, exist_ok=True)
        json_path = destination / f"{proposal.proposal_id}.json"
        markdown_path = destination / f"{proposal.proposal_id}.md"
        atomic_write_json(json_path, proposal.to_dict())
        comparison = proposal.comparison.to_dict() if proposal.comparison else {}
        lines = [
            f"# Resource update proposal {proposal.proposal_id}",
            "",
            f"- Status: `{proposal.status.value}`",
            f"- Resource: `{proposal.resource_id}` ({proposal.resource_kind.value})",
            f"- Candidate version: `{proposal.candidate_version}`",
            f"- Source: `{proposal.source_url}`",
            f"- Source SHA-256: `{proposal.observed_source_sha256 or '(not verified)'}`",
            f"- Candidate SHA-256: `{proposal.candidate_sha256 or '(not built)'}`",
            f"- Redistribution: `{proposal.redistribution_state.value}`",
            f"- Active environment changed automatically: `False`",
            "",
            "## Build/test evidence",
            "",
        ]
        for item in (*proposal.build_evidence, *proposal.test_evidence):
            lines.append(f"- `{item.evidence_id}`: {item.status.value} — {item.details}")
        lines.extend(["", "## Findings", ""])
        for finding in proposal.findings:
            state = "resolved" if finding.resolved else "unresolved"
            lines.append(f"- `{finding.severity.value}` `{state}` {finding.finding_id}: "
                         f"{finding.summary}")
        lines.extend(["", "## Comparison", "", "```json",
                      json.dumps(comparison, sort_keys=True, indent=2), "```", ""])
        atomic_write_bytes(markdown_path, ("\n".join(lines)).encode("utf-8"))
        return json_path, markdown_path


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m flyash_phreeqc_ml.resource_steward")
    parser.add_argument("--store-root", required=True,
                        help="Resource volume root (outside Git for installed resources)")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check")
    check.add_argument("--resource-id", required=True)
    check.add_argument("--kind", required=True, choices=[item.value for item in ResourceKind])
    check.add_argument("--candidate-version", required=True)
    check.add_argument("--source-url", required=True)
    check.add_argument("--archive-filename", required=True)
    check.add_argument("--expected-sha256", default="")
    check.add_argument("--rights-notice", default="")
    check.add_argument("--redistribution-state", default=RedistributionState.UNKNOWN.value,
                       choices=[item.value for item in RedistributionState])

    show = sub.add_parser("show")
    show.add_argument("proposal_id", nargs="?")
    download = sub.add_parser("download-candidate")
    download.add_argument("proposal_id")
    verify = sub.add_parser("verify-candidate")
    verify.add_argument("proposal_id")
    verify.add_argument("--rights-notice")
    verify.add_argument("--redistribution-state",
                        choices=[item.value for item in RedistributionState])
    build = sub.add_parser("build-candidate")
    build.add_argument("proposal_id")
    build.add_argument(
        "--archive-member",
        help="Exact .dat member selected from an already verified multi-database archive",
    )
    test = sub.add_parser("test-candidate")
    test.add_argument("proposal_id")
    compare = sub.add_parser("compare")
    compare.add_argument("proposal_id")
    promote = sub.add_parser("promote")
    promote.add_argument("proposal_id")
    promote.add_argument("--candidate-sha256", required=True)
    promote.add_argument("--admin-confirmation", required=True)
    rollback = sub.add_parser("rollback")
    rollback.add_argument("proposal_id")
    rollback.add_argument("--admin-confirmation", required=True)
    export = sub.add_parser("export-report")
    export.add_argument("proposal_id")
    export.add_argument("--output-dir", required=True)
    bootstrap = sub.add_parser("bootstrap-release")
    bootstrap.add_argument("--runtime-manifest", required=True)
    bootstrap.add_argument("--executable", required=True)
    bootstrap.add_argument("--database-dir", required=True)
    bootstrap.add_argument("--registry")
    bootstrap.add_argument("--knowledge-output-dir")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    steward = ResourceSteward(CatalogStore(args.store_root))
    try:
        if args.command == "check":
            result = steward.check(
                resource_id=args.resource_id,
                resource_kind=ResourceKind(args.kind),
                candidate_version=args.candidate_version,
                source_url=args.source_url,
                archive_filename=args.archive_filename,
                expected_source_sha256=args.expected_sha256,
                rights_notice=args.rights_notice,
                redistribution_state=RedistributionState(args.redistribution_state),
            ).to_dict()
        elif args.command == "show":
            shown = steward.show(args.proposal_id)
            result = ([item.to_dict() for item in shown] if isinstance(shown, tuple)
                      else shown.to_dict())
        elif args.command == "download-candidate":
            result = steward.download_candidate(args.proposal_id).to_dict()
        elif args.command == "verify-candidate":
            state = (RedistributionState(args.redistribution_state)
                     if args.redistribution_state else None)
            result = steward.verify_candidate(
                args.proposal_id, rights_notice=args.rights_notice,
                redistribution_state=state).to_dict()
        elif args.command == "build-candidate":
            result = steward.build_candidate(
                args.proposal_id, archive_member=args.archive_member).to_dict()
        elif args.command == "test-candidate":
            result = steward.test_candidate(args.proposal_id).to_dict()
        elif args.command == "compare":
            result = steward.compare(args.proposal_id).to_dict()
        elif args.command == "promote":
            result = steward.promote(
                args.proposal_id,
                candidate_sha256=args.candidate_sha256,
                admin_confirmation=args.admin_confirmation,
                actor_role="admin",
                actor_type="human",
            ).to_dict()
        elif args.command == "rollback":
            result = steward.rollback(
                args.proposal_id,
                admin_confirmation=args.admin_confirmation,
                actor_role="admin",
                actor_type="human",
            ).to_dict()
        elif args.command == "bootstrap-release":
            result = bootstrap_release_resources(
                steward.store,
                runtime_manifest=args.runtime_manifest,
                executable_path=args.executable,
                database_directory=args.database_dir,
                registry_path=args.registry,
                knowledge_output_directory=args.knowledge_output_dir,
            ).to_dict()
        else:
            paths = steward.export_report(args.proposal_id, args.output_dir)
            result = {"json_report": str(paths[0]), "human_report": str(paths[1])}
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (StewardError, CatalogError, ReleaseBootstrapError,
            ResourceContractError, ValueError) as exc:
        print(f"resource steward error: {exc}", file=sys.stderr)
        return 2
