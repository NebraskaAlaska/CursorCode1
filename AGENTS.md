# WPI Virtual LAB — Codex Instructions

## Before major work

- Verify the current Git branch and working-tree state. Work only on the current Codex branch unless the user explicitly instructs otherwise.
- Never merge `main` automatically. Never push, open a pull request, commit, rebase, or switch branches without explicit permission.
- Resolve the shared Obsidian context from `~/Library/Application Support/obsidian/obsidian.json`; select the existing `Obsidian (AI Testing)` location that contains `.obsidian`, `AI Council`, and `WPI Project`. Do not create another vault or modify `.obsidian` or `AI Council`.
- Read the relevant `WPI Project` notes before substantial design, scientific, architecture, or workflow changes.
- Repository source, Git state, tests, and deterministic evidence outrank stale Obsidian documentation.
- Treat the Obsidian `AI Council` snapshot as reference material; never copy or integrate it into this repository unless a task explicitly requests that work.

## Scientific contract

- Never invent composition, measurements, simulation output, literature evidence, validation status, model predictions, or metrics.
- Keep measured, simulated, inferred, advisory, literature-derived, synthetic-demo, and ML-predicted information visibly distinct.
- PHREEQC input must be previewed and reviewed before an explicitly confirmed execution. Simulation is not experimental validation, and unavailable execution must never be reported as successful.
- Preserve ICP units, dilution and blank corrections, sample mapping, QC flags, and provenance.
- Keep XRD output approximate/advisory and tentative; never present a candidate match as confirmed phase identification.
- Only a trained model may produce an ML prediction. Keep training, out-of-sample evaluation, prediction, uncertainty, model cards, and provenance distinct; report only metrics calculated from data.
- Preserve literature provenance where available: title, authors, year, DOI, URL, source location, and extraction confidence.

## Verification and handoff

- Run tests appropriate to every source change and record the commands and actual results, never an expected or historical count.
- Historical test counts are baselines, not permanent acceptance counts; legitimate tests may increase the suite.
- After every meaningful completed task, update the relevant `WPI Project` notes and append—never overwrite—an entry to `WPI Project/99 - Handoff Log.md` using its required format.
- Council Reviewer approval never authorizes committing, pushing, merging, deploying, publishing, or promoting.
- Never expose, copy, log, or store secrets, credentials, `.env` content, API keys, tokens, cookies, authentication databases, or private session material.
