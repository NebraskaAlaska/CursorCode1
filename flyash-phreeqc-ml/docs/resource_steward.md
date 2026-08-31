# Resource Steward and database-extension lifecycle

The Resource Steward is the deterministic, Streamlit-independent authority for scientific
software and database candidates. A scheduled monitor, UI, LLM, or successful download cannot
activate a resource. Candidate work is side-by-side and inactive until its exact evidence is
reviewed and a human supplies the required proposal/hash confirmation.

## Closed candidate sequence

Every candidate follows `check` → `download-candidate` → `verify-candidate` → `build-candidate` →
`test-candidate` → `compare`. Promotion and rollback are separate exact-confirmation operations.
The Steward preserves resource/version/content identity, evidence, findings, active and prior
manifests, and reference records. Failed verification or testing never changes the active resource.

Archives and files are bounded, hash-checked, regular and non-symlink, and resolved inside the
candidate workspace. Rights and redistribution state remain explicit. Database or model bytes,
credentials, user data, and generated scientific results are never release-source artifacts.

## Sealed database-extension test

An extension manifest records:

- extension resource ID, installation ID, version, filename, and SHA-256;
- required base resource ID, installation ID, version, exact filename, family, and SHA-256;
- a canonical combined identity over both resources;
- direct compatibility, authoritative project-integration, and identity-binding evidence.

The Steward must resolve one and only one exact base. It rereads stable verified bytes for both
files, refuses symlinks/traversal/changed bytes, rejects an extension containing an external
`INCLUDE` directive, and builds one self-contained input by appending a fixed Steward-owned smoke
template to the exact extension text. The base stays the PHREEQC database argument; no database
concatenation occurs. The exact input then passes both direct PHREEQC execution and the ordinary
project preview/review/confirmation/executor contract. Run provenance carries the base, extension,
combined, reviewed-input, and executor-environment identities.

The ordinary executor still rejects every `INCLUDE` and `INCLUDE$`. Users cannot name or traverse
to an external target. A tested extension cannot be rebound in place; changed extension or base
bytes invalidate testing and promotion. Explicit promotion records the previous manifest, rollback
restores it, and old run references remain tied to their original installation identity.

Tests use synthetic extension fixtures in an isolated resource store. They do not promote or
modify the bundled PHREEQC/database installation and do not write extension bytes to Git or
Obsidian. Real PHREEQC execution of that synthetic lifecycle is required in the release test image
and in both architecture jobs; host-only runs skip it when PHREEQC is unavailable.
