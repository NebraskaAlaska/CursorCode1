#!/usr/bin/env python3
"""Inspect official USGS/Empa metadata and emit a non-mutating update report."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import ssl
import sys
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, HTTPSHandler


USGS_PAGE = "https://water.usgs.gov/water-resources/software/PHREEQC/"
EMPA_PAGE = "https://www.empa.ch/web/s308/thermodynamic-data"
ALLOWED_HOSTS = frozenset({"water.usgs.gov", "www.usgs.gov", "www.empa.ch", "empa.ch"})
MAX_METADATA_BYTES = 2 * 1024 * 1024


class MonitorError(RuntimeError):
    pass


class CheckedRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        absolute = urljoin(request.full_url, newurl)
        validate_url(absolute)
        return super().redirect_request(request, fp, code, msg, headers, absolute)


def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise MonitorError("official metadata request left the HTTPS host allow-list")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise MonitorError("official metadata URL contains credentials or a non-HTTPS port")


def fetch(url: str) -> tuple[bytes, dict[str, str], str]:
    validate_url(url)
    opener = build_opener(HTTPSHandler(context=ssl.create_default_context()), CheckedRedirects())
    response = opener.open(
        Request(url, headers={"User-Agent": "WPI-Virtual-LAB-update-monitor/1"}), timeout=30
    )
    final_url = response.geturl()
    validate_url(final_url)
    declared = response.headers.get("Content-Length")
    if declared and int(declared) > MAX_METADATA_BYTES:
        raise MonitorError("official metadata page exceeds the size limit")
    payload = response.read(MAX_METADATA_BYTES + 1)
    if len(payload) > MAX_METADATA_BYTES:
        raise MonitorError("official metadata page exceeds the size limit")
    headers = {
        key: response.headers.get(key, "")
        for key in ("ETag", "Last-Modified", "Content-Type")
    }
    return payload, headers, final_url


def decode_html(payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MonitorError("official metadata page is no longer UTF-8") from exc


def version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.split(r"[.-]", value))


def parse_usgs(payload: bytes) -> dict[str, object]:
    html = decode_html(payload)
    versions = sorted(
        set(re.findall(r"phreeqc-(\d+\.\d+\.\d+-\d+)\.tar\.gz", html, re.IGNORECASE)),
        key=version_key,
    )
    if not versions:
        raise MonitorError("USGS page format changed: no PHREEQC source archive metadata found")
    current = versions[-1]
    filenames = sorted(set(re.findall(
        rf"phreeqc-{re.escape(current)}\.tar\.gz", html, re.IGNORECASE
    )))
    if filenames != [f"phreeqc-{current}.tar.gz"]:
        raise MonitorError("USGS current archive metadata is ambiguous")
    return {"detected_version": current, "archive_filename": filenames[0]}


def parse_empa(payload: bytes) -> dict[str, object]:
    html = decode_html(payload)
    versions = sorted(
        set(re.findall(r"\bCEMDATA\s*([0-9]+(?:\.[0-9]+)+)\b", html, re.IGNORECASE)),
        key=version_key,
    )
    if not versions:
        raise MonitorError("Empa page format changed: no CEMDATA version metadata found")
    return {
        "detected_version": versions[-1],
        "redistribution_state": "external_only_pending_explicit_rights_evidence",
    }


def source_report(
    name: str, url: str, payload: bytes, headers: dict[str, str], final_url: str, parsed: dict
) -> dict[str, object]:
    return {
        "name": name,
        "requested_url": url,
        "final_url": final_url,
        "metadata_sha256": hashlib.sha256(payload).hexdigest(),
        "headers": headers,
        **parsed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--usgs-fixture", type=Path)
    parser.add_argument("--empa-fixture", type=Path)
    args = parser.parse_args()
    report: dict[str, object] = {
        "schema": "wpi.virtual-lab.official-update-monitor-report",
        "version": 1,
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "baseline": {"phreeqc": "3.8.6-17100", "cemdata": "18.11"},
        "constraints": {
            "active_installation_unchanged": True,
            "downloaded_candidate": False,
            "deployed": False,
            "redistributed_external_database": False,
        },
        "sources": [],
        "status": "manual_review_required",
    }
    try:
        if bool(args.usgs_fixture) != bool(args.empa_fixture):
            raise MonitorError("both fixtures are required together")
        if args.usgs_fixture:
            usgs = (args.usgs_fixture.read_bytes(), {}, USGS_PAGE)
            empa = (args.empa_fixture.read_bytes(), {}, EMPA_PAGE)
        else:
            usgs = fetch(USGS_PAGE)
            empa = fetch(EMPA_PAGE)
        sources = [
            source_report("USGS PHREEQC", USGS_PAGE, *usgs, parse_usgs(usgs[0])),
            source_report("Empa CEMDATA", EMPA_PAGE, *empa, parse_empa(empa[0])),
        ]
        report["sources"] = sources
        detected = {item["name"]: item["detected_version"] for item in sources}
        report["status"] = (
            "no_version_change_detected"
            if detected == {"USGS PHREEQC": "3.8.6-17100", "Empa CEMDATA": "18.11"}
            else "candidate_metadata_change_requires_manual_review"
        )
    except (MonitorError, OSError, ValueError) as exc:
        report["error"] = str(exc)
        report["status"] = "parser_or_official_page_failure_requires_manual_review"
        exit_code = 2
    else:
        exit_code = 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
