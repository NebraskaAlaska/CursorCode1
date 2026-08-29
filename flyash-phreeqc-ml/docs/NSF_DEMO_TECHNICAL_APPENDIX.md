# NSF demo technical appendix

Deterministic identity:

- USGS PHREEQC `3.8.6-17100`
- source `https://water.usgs.gov/water-resources/software/PHREEQC/phreeqc-3.8.6-17100.tar.gz`
- archive SHA-256 `b5c4a6dfea1a6bb6a3436857a50346bb943904a49582714494b4f1b1e54e64e1`
- `phreeqc.dat` SHA-256 `59373961d648dfbf68a40744060c1d64f57ecbec98f4f5fb89f3a1b4213ccd10`
- database `/opt/phreeqc/database/phreeqc.dat`
- examples `/opt/phreeqc/share/examples/`
- source manifest and full USGS notice under `release/`

The builder rejects transfer, digest, unsafe archive path/link, database, and notice mismatches; then compiles the CLI and runs official example `ex1`. The official input/database set remains available for diagnostics.

The final image copies explicit source/release paths. `.dockerignore` excludes raw data, workspaces, downloaded resources, external databases, model binaries, credentials, TLS keys, backups, and logs. The test target adds tests and developer dependencies but never `data/raw`. Runtime uses UID 10001, root-owned read-only code, bounded temporary storage, dropped capabilities, and exactly five writable durable roots.

`release/RELEASE_MANIFEST.template.json` begins `candidate_unverified`. The generator records Git state and only caller-supplied digest/evidence; it cannot infer a pass. The official example, application tests, scientific-contract tests, Compose/security checks, persistence, and demo acceptance are distinct evidence.
