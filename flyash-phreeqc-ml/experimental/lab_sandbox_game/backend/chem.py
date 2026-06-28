"""Deterministic chemistry helpers for the sandbox backend — stoichiometry ONLY.

This module does exactly one honest thing: it parses a chemical *formula* into element counts and
computes a molar mass from standard atomic weights. That is pure arithmetic over stoichiometry.

What it deliberately does **not** do — and what the rest of the backend must never ask it to:

* It does **not** infer phases, crystal structure, or a diffraction pattern from a formula. Knowing
  that ``SiO2`` contains one Si and two O tells you nothing about whether it is quartz, cristobalite,
  or amorphous glass. Phase/structure come only from a reference, never from stoichiometry.
* It does **not** invent data for an unparseable string. A name like ``"Unobtainium"`` raises
  :class:`FormulaParseError`; the synthesizer then returns an honest *unknown* card.

Atomic weights are the IUPAC conventional/standard values, rounded; they are physical constants, not
fabricated measurements. The set is intentionally small (the elements this project touches).
"""
from __future__ import annotations

import re

# IUPAC conventional atomic weights (standard values), rounded. Physical constants.
ATOMIC_WEIGHTS = {
    "H": 1.008, "C": 12.011, "N": 14.007, "O": 15.999, "F": 18.998,
    "Na": 22.990, "Mg": 24.305, "Al": 26.982, "Si": 28.085, "P": 30.974,
    "S": 32.06, "Cl": 35.45, "K": 39.098, "Ca": 40.078, "Ti": 47.867,
    "V": 50.942, "Cr": 51.996, "Mn": 54.938, "Fe": 55.845, "Ni": 58.693,
    "Cu": 63.546, "Zn": 65.38, "Sr": 87.62, "Ba": 137.327,
    # Rare earths / Sc + Y that the ICP module also reports.
    "Sc": 44.956, "Y": 88.906, "La": 138.905, "Ce": 140.116, "Nd": 144.242,
}

_ELEMENT_TOKEN = re.compile(r"[A-Z][a-z]?")


class FormulaParseError(ValueError):
    """Raised when a string is not a parseable chemical formula (so callers can stay honest)."""


def parse_formula(formula) -> dict:
    """Parse ``formula`` into ``{element_symbol: integer_count}``. Strict — garbage raises.

    Supports nested parentheses (``Ca(OH)2``) and hydrate dots (``CaSO4·2H2O`` / ``CaSO4.2H2O`` /
    ``CaSO4*2H2O``) with an optional leading coefficient on each dotted segment. Any character that is
    not part of a valid token causes a :class:`FormulaParseError` — we never "best-effort" garbage
    into a fake composition. Element *symbols* are accepted structurally even if they are not in
    :data:`ATOMIC_WEIGHTS`; :func:`molar_mass` is where an unknown element is rejected.
    """
    s = (formula or "").strip()
    if not s:
        raise FormulaParseError("empty formula")
    # Normalise hydrate separators to '.'.
    for dot in ("·", "•", "∙", "*", "·"):
        s = s.replace(dot, ".")

    total: dict[str, int] = {}
    for segment in s.split("."):
        seg = segment.strip()
        if not seg:
            continue
        coeff = 1
        m = re.match(r"^(\d+)(.+)$", seg)
        if m and re.match(r"[A-Za-z(]", m.group(2)):
            coeff = int(m.group(1))
            seg = m.group(2)
        for el, cnt in _parse_group(seg).items():
            total[el] = total.get(el, 0) + cnt * coeff

    if not total:
        raise FormulaParseError(f"no elements found in {formula!r}")
    return total


def _parse_group(s: str) -> dict:
    """Stack-based parse of one parenthesised expression (no hydrate dots) into element counts."""
    stack: list[dict] = [{}]
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "(":
            stack.append({})
            i += 1
        elif c == ")":
            i += 1
            j = i
            while j < n and s[j].isdigit():
                j += 1
            mult = int(s[i:j]) if j > i else 1
            i = j
            if len(stack) < 2:
                raise FormulaParseError(f"unbalanced ')' in {s!r}")
            grp = stack.pop()
            top = stack[-1]
            for el, cnt in grp.items():
                top[el] = top.get(el, 0) + cnt * mult
        elif c.isspace():
            i += 1
        else:
            m = _ELEMENT_TOKEN.match(s, i)
            if not m:
                raise FormulaParseError(f"unexpected character {c!r} in {s!r}")
            el = m.group(0)
            i = m.end()
            j = i
            while j < n and s[j].isdigit():
                j += 1
            cnt = int(s[i:j]) if j > i else 1
            i = j
            stack[-1][el] = stack[-1].get(el, 0) + cnt
    if len(stack) != 1:
        raise FormulaParseError(f"unbalanced '(' in {s!r}")
    return stack[0]


def molar_mass(formula) -> float:
    """Molar mass (g/mol) of ``formula`` from :data:`ATOMIC_WEIGHTS`.

    An element symbol with no known atomic weight raises :class:`FormulaParseError` — we report that
    we cannot compute it rather than guessing a mass. A bare element symbol (``"Ca"``) works too.
    """
    counts = parse_formula(formula)
    unknown = sorted(el for el in counts if el not in ATOMIC_WEIGHTS)
    if unknown:
        raise FormulaParseError(f"no atomic weight for element(s): {', '.join(unknown)}")
    return round(sum(ATOMIC_WEIGHTS[el] * cnt for el, cnt in counts.items()), 4)
