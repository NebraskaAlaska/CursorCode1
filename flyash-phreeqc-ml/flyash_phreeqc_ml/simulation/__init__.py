"""Natural-language **simulation planning** layer + a gated **execution** layer.

This package turns a free-text experiment description into a *structured, reviewable
scenario*, a *simulation plan/matrix*, and a deterministic *PHREEQC input preview*. The
**planning** modules (:mod:`scenario_schema`, :mod:`safety`, :mod:`rule_parser`,
:mod:`matrix`, :mod:`phreeqc_input_builder`) are deliberately a planning layer only:

* They never run PHREEQC, never overwrite measured data, and never produce a verified
  result. A generated matrix is labelled "Simulation plan only — no PHREEQC result has
  been generated yet."
* The scientific safety analysis (missing fields, caveats, leachant support) is computed
  by deterministic code in :mod:`safety`, not by the AI — the AI only *extracts* a
  scenario the user then reviews and confirms.

:mod:`phreeqc_executor` is a **separate execution layer** that *does* run PHREEQC, but only
when the user explicitly asks (after reviewing the input preview), only if a binary +
database are configured, and only into a safe ``outputs/simulations/`` workspace. Its
outputs are **simulation results, not validated predictions**. Both layers are off the
scientific result path: nothing here feeds mapping status, residuals, validation status, or
the comparison data, and neither imports an AI helper or a comparison module.

The AI extractor lives in :mod:`flyash_phreeqc_ml.ai.scenario_parser` and falls back to
the rule-based :mod:`rule_parser` here when AI is disabled.
"""
