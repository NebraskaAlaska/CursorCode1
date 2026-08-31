#!/usr/bin/env python3
"""Materialize a release-evidence manifest without inventing verification claims."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def git_value(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--image-reference")
    parser.add_argument("--image-digest", help="OCI sha256 digest produced by a completed build")
    parser.add_argument("--test-evidence", action="append", default=[])
    args = parser.parse_args()
    if args.image_digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", args.image_digest):
        parser.error("--image-digest must be an exact lower-case OCI sha256 digest")
    if args.image_reference and "REPLACE_WITH" in args.image_reference.upper():
        parser.error("--image-reference must not contain a placeholder")

    manifest = json.loads(
        (ROOT / "release" / "RELEASE_MANIFEST.template.json").read_text(encoding="utf-8")
    )
    manifest["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    manifest["git_commit"] = git_value("rev-parse", "HEAD")
    manifest["git_tree_state"] = "dirty" if git_value("status", "--porcelain") else "clean"
    manifest["image"]["reference"] = args.image_reference or None
    manifest["image"]["digest"] = args.image_digest or None
    manifest["test_evidence"] = args.test_evidence
    if args.image_digest and args.test_evidence:
        manifest["release_status"] = "evidence_recorded_not_promoted"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
