# Data safety

What the app keeps private, what it can write, and what leaves your machine.

## Your data stays local by default

- **Runs** live under `experiments/<your_run>/` on your machine. Measured CSVs, generated
  outputs, figures, mappings, and the per-run audit log are **gitignored** — they are not
  committed to version control unless you deliberately force them in.
- **Raw research data** under `data/raw/` is treated as potentially confidential. Do not
  commit a new raw dataset unless you've confirmed it's allowed.
- **Generated artifacts** (processed CSVs, figures, the experiment plan, the scenario
  manifest, validation-report folders, trained models, surrogate datasets) are gitignored
  and re-creatable by running the workflow.

## What the optional online features send

The core app is fully offline. Two **optional** features call an external API, and only
when you enable them (an API key) and consent in the app:

- **AI import-assist** — sends your column headers and a small preview (not the full
  dataset) to suggest a column mapping. Every suggestion lands in the review step for you
  to accept or reject; nothing is saved automatically.
- **Ask the assistant** — sends your question plus **numeric summaries** of the selected
  run (counts, statuses, hashes — never measured values or file contents) to answer
  grounded questions. It is read-only and never changes your data.
- **Literature retrieval** — sends your search request (the quantity, material, and
  conditions) for a sourced web search; every result is quarantined until you confirm it.

Each shows a one-time notice and a consent checkbox before anything leaves the machine, and
all stay hidden if no API key is configured.

## Your API key

Your Anthropic API key is read **only** from the `ANTHROPIC_API_KEY` environment variable
or a Streamlit secret — it is never entered in, displayed by, logged by, or stored by the
app. The sidebar **🤖 AI settings** panel reports only whether a key was *detected* (yes/no)
and its *source*, never the key itself. AI output is **suggestion / interpretation only**
and cannot change mapping, residuals, validation status, or the comparison data — AI cannot
validate the science by itself. Setup details: [`ai_configuration.md`](../ai_configuration.md).

## The audit log records actions, not data

Every run keeps an **append-only** audit log (`experiments/<run>/outputs/audit_log.jsonl`)
of *actions*: imports, mappings accepted/deleted, workflow runs, comparison generation,
exports. It stores names, counts, ids, statuses, and content hashes — **never measured
values and never file contents**. The data itself lives in the run's CSVs; the log records
what you did to it.

## The validation report

The **Export** tab builds a self-contained report folder you can hand to a reviewer. It
contains your run's data and results — share it only with people allowed to see that data.
The folder is a normal run output (gitignored).

For the exact gitignore rules and the confidential-data policy, see the project's
`CLAUDE.md` and `.gitignore`.
