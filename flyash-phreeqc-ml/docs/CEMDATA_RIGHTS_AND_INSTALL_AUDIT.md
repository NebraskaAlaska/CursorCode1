# CEMDATA rights and installation audit

## Task-time evidence (2026-08-29)

- Official publisher page inspected: [Empa — Thermodynamic Data](https://www.empa.ch/web/s308/thermodynamic-data).
- Page identity observed: Empa, Laboratory for Concrete & Asphalt, thermodynamic-data page.
- Release label observed in the official page/search metadata: `Cemdata18.11`.
- An exact official archive URL, archive filename, content length, checksum, or signed release manifest was **not observed** in the evidence available to this task.
- A direct automated request to the official page returned HTTP 403. That result was treated as access unavailable, not bypassed with session/WAF material and not guessed around with a mirror.
- Because no archive was obtained, archive members, database headers, README, licence file, and embedded rights text were **not inspected**. Their status is `not_observed`, not “absent.”
- Repository and container scans found no CEMDATA database payload. No third-party mirror was used.

The reviewed evidence identifies an official CEMDATA release family, but it does not establish explicit permission for this project to redistribute its database bytes. The managed-resource policy is therefore `external_only`. This is intentionally narrower than claiming that redistribution is permitted or prohibited.

## Enforced consequences

- No CEMDATA bytes are committed, mirrored, bundled in an image, uploaded as a release artifact, or copied into documentation or the knowledge pack.
- The update monitor may inspect only exact allowlisted official Empa metadata. A changed/unreadable page fails visibly and requires manual review; it does not infer a version or download URL.
- A local human user, or a hosted human administrator, must obtain the file from the official source and record the exact page, archive filename, source hash, version evidence, citation, and reviewed rights/licence basis.
- Import hashes the exact archive and selected database member, rejects unsafe archive structure, performs bounded basic secret/malware checks, parses one selected database, and installs it outside Git in the durable resource volume.
- Ordinary hosted users cannot install server databases.
- Import is side-by-side and inactive. External/add-on databases are never concatenated with another database, and a previously confirmed run remains bound to its original runtime/database hashes.
- Activation still requires completed compatibility tests, an exact proposal/candidate hash, no unresolved high/medium finding, and an explicit human administrator confirmation.

## What would change this status

New explicit rights evidence must be captured from an exact official source and tied to the exact archive hash. The archive/header/README/licence inspection must be repeated and recorded. Even then, rights evidence alone does not authorize activation, redistribution, deployment, publishing, or a Git operation.
