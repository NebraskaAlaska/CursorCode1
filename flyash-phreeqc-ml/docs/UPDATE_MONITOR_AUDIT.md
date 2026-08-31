# Scheduled update-monitor audit

`.github/workflows/resource-update-monitor.yml` runs weekly and manually with read-only repository permissions. It invokes `scripts/monitor_official_resources.py`, which fetches bounded UTF-8 metadata only from exact allowlisted HTTPS USGS/Empa hosts, validates every redirect, extracts PHREEQC and CEMDATA version metadata with strict parsers, and uploads a JSON report for human review.

It does not download a candidate archive/database, modify the catalog or branch, activate a resource, build/deploy an image, open an issue, or redistribute CEMDATA. CEMDATA remains `external_only` pending explicit rights evidence. A version difference is a review signal, not an update. A missing/ambiguous version, unexpected encoding/size, disallowed redirect, or page/parser change exits nonzero while still retaining the failure report.

The fixture path in `tests/test_release_distribution_phase4.py` proves both expected metadata and visible parser failure without network access. Live weekly results are external evidence and must not be represented as a release pass until a human reviews them.
