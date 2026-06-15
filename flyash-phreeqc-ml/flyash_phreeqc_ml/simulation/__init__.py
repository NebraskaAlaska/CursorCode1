"""Natural-language **simulation planning** layer (no PHREEQC execution, no ML).

This package turns a free-text experiment description into a *structured, reviewable
scenario* and a *simulation plan/matrix*. It is deliberately a **planning layer only**:

* It never runs PHREEQC, never overwrites measured data, and never produces a verified
  result. A generated matrix is labelled "Simulation plan only — no PHREEQC result has
  been generated yet."
* The scientific safety analysis (missing fields, caveats, leachant support) is computed
  by deterministic code in :mod:`safety`, not by the AI — the AI only *extracts* a
  scenario the user then reviews and confirms.
* It is off the scientific result path: nothing here feeds mapping status, residuals,
  validation status, or the comparison data.

The AI extractor lives in :mod:`flyash_phreeqc_ml.ai.scenario_parser` and falls back to
the rule-based :mod:`rule_parser` here when AI is disabled.
"""
