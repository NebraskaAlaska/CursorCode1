"""Deterministic validation for a :class:`MaterialProfile` (pure; no AI, no I/O).

Turns a (possibly half-filled, possibly literature-sourced) composition profile into a
:class:`ProfileValidationResult` of errors / warnings / infos, and decides — by code, not
by the AI — whether the profile may be confirmed and whether it is *already* usable for a
PHREEQC input preview.

Hard rules:

* A missing material name, an unknown basis, a negative value, or no resolvable element is
  an **error** (``ok`` is False; the profile cannot be confirmed).
* An implausible oxide total, an out-of-range wt %, an unrecognized species, or a basis
  conversion is a **warning / info** — surfaced, never silently fixed.
* A ``literature_unverified`` profile is **never** usable until a human confirms it
  (``requires_confirmation`` is True, ``usable_for_preview`` is False) — the AI/literature
  quarantine rule, enforced here.
"""
from __future__ import annotations

from . import profile_schema as S
from .profile_schema import MaterialProfile, ProfileValidationResult

# A single wt% above this is almost certainly a typo / wrong basis.
_MAX_PLAUSIBLE_WT = 100.0
# mg/kg above this exceeds 100 wt% — impossible.
_MAX_PLAUSIBLE_MG_PER_KG = 1.0e6


def validate_profile(profile: MaterialProfile) -> ProfileValidationResult:
    """Validate a material composition profile. Never raises."""
    res = ProfileValidationResult()

    # 1) required identity ------------------------------------------------- #
    if not (profile.material_name and str(profile.material_name).strip()):
        res.errors.append("Material name is required (give the material a name).")
    if not (profile.material_type and str(profile.material_type).strip()):
        res.warnings.append("Material type is not set — recommended (e.g. "
                            "'class_c_fly_ash', 'bauxite_residue', 'generic').")

    # 2) known basis ------------------------------------------------------- #
    basis = profile.composition_basis
    if basis not in S.KNOWN_BASES:
        res.errors.append(
            f"Composition basis {basis!r} is not recognized — use one of: "
            + ", ".join(S.KNOWN_BASES) + ".")
        # Without a known basis nothing else can be checked meaningfully.
        return _finalize(profile, res)

    # 3) entries present --------------------------------------------------- #
    if not profile.entries:
        res.errors.append("No composition entries — add at least one component.")

    # 4) per-entry checks (negative / impossible / unrecognized) ----------- #
    resolved_count = 0
    for entry in profile.entries:
        species = str(entry.species or "").strip()
        if not species:
            res.warnings.append("An entry has no species name and was ignored.")
            continue
        v = entry.numeric_value()
        if v is None:
            res.warnings.append(f"Entry '{species}' has no numeric value and was ignored.")
            continue
        if v < 0:
            res.errors.append(f"Entry '{species}' is negative ({v}); negative composition "
                              "values are not allowed.")
            continue
        # impossible-value warnings (basis-aware)
        if basis in (S.BASIS_OXIDE_WT, S.BASIS_ELEMENT_WT) and v > _MAX_PLAUSIBLE_WT:
            res.warnings.append(f"Entry '{species}' = {v} wt % exceeds 100 % — check the "
                                "value or the basis.")
        if basis == S.BASIS_MG_PER_KG and v > _MAX_PLAUSIBLE_MG_PER_KG:
            res.warnings.append(f"Entry '{species}' = {v} mg/kg exceeds 100 wt % "
                                "(1,000,000 mg/kg) — check the value.")
        el, _wt, status = S.resolve_entry(entry, basis)
        if status == "resolved":
            resolved_count += 1
        elif status == "non_element":
            res.infos.append(f"'{species}' treated as LOI / moisture / total — included in "
                             "the sum, not as an element.")
        elif status == "unrecognized":
            hint = ("not a recognized oxide" if basis == S.BASIS_OXIDE_WT
                    else "not a recognized element symbol")
            res.warnings.append(f"Species '{species}' is {hint}; it will be ignored when "
                                "building element composition.")
    res.n_elements_resolved = resolved_count
    if profile.entries and resolved_count == 0:
        res.errors.append("No entry resolved to a known element — the composition cannot be "
                          "used. Check the basis and the species names.")

    # 5) oxide-sum plausibility ------------------------------------------- #
    if basis == S.BASIS_OXIDE_WT:
        total = profile.oxide_total()
        res.oxide_total = total
        if total is not None:
            if total < S.OXIDE_SUM_MIN:
                res.warnings.append(
                    f"Oxide total is {total:.1f} wt % (+ LOI/moisture) — below "
                    f"{S.OXIDE_SUM_MIN:.0f} %. Components may be missing or LOI/moisture not "
                    "entered; element fractions will be under-counted.")
            elif total > S.OXIDE_SUM_MAX:
                res.warnings.append(
                    f"Oxide total is {total:.1f} wt % — above {S.OXIDE_SUM_MAX:.0f} %. Check "
                    "for a duplicated component or a wrong value.")
            else:
                res.infos.append(f"Oxide total {total:.1f} wt % (incl. LOI/moisture) — "
                                 "within the plausible range.")

    # 6) LOI / moisture clarity ------------------------------------------- #
    if profile.loi_pct is not None or profile.moisture_pct is not None:
        res.infos.append("LOI / moisture recorded — included in the oxide total but not "
                         "converted to an element.")
    elif basis == S.BASIS_OXIDE_WT:
        res.infos.append("No LOI / moisture entered — if the assay reports one, add it so "
                         "the total is interpretable.")

    # 7) basis-conversion note -------------------------------------------- #
    if basis == S.BASIS_OXIDE_WT:
        res.infos.append("Oxide wt % will be converted to element wt % using gravimetric "
                         "factors (e.g. CaO → Ca × 0.715).")
    elif basis == S.BASIS_MG_PER_KG:
        res.infos.append("mg/kg will be converted to element wt % (÷ 10,000).")
    elif basis == S.BASIS_MOL_PER_KG:
        res.infos.append("mol/kg will be converted to element wt % (× molar mass ÷ 10).")

    return _finalize(profile, res)


def _finalize(profile: MaterialProfile, res: ProfileValidationResult) -> ProfileValidationResult:
    """Set ``ok`` / ``can_confirm`` / ``usable_for_preview`` / ``requires_confirmation``."""
    res.ok = not res.errors
    res.can_confirm = res.ok
    res.requires_confirmation = (
        profile.verification_status == S.STATUS_LITERATURE_UNVERIFIED)
    # Usable for a preview only when it validates AND already carries a usable status.
    res.usable_for_preview = res.ok and profile.verification_status in S.USABLE_STATUSES
    if res.requires_confirmation and res.ok:
        res.warnings.append(
            "This composition is literature-sourced and unverified — review every value and "
            "citation, then confirm it before it can be used for an input preview.")
    return res
