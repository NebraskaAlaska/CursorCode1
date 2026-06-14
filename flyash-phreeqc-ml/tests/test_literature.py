"""Tests for AI-assisted literature retrieval (ai/literature.py) — client + search mocked.

Pins (per the spec): uncited / quote-less candidates are dropped *in code*; a DOI is
normalised to a doi.org link; a URL-only candidate is accepted; the quarantine is real
(an unconfirmed value never reaches mass_balance); confirming a conditions-mismatched
value needs a second acknowledgement; the honest "no value found" path; and the feature
disables cleanly with no API key. Synthetic only — no network, the client is always fake.
"""
from __future__ import annotations

import types

import pytest

from flyash_phreeqc_ml import audit, config, mass_balance, profiles, run_manager, units
from flyash_phreeqc_ml.ai import import_assist
from flyash_phreeqc_ml.ai import literature as L


# --------------------------------------------------------------------------- #
# Fixtures: a temp run + a batch profile that opts into mass balance
# --------------------------------------------------------------------------- #
@pytest.fixture()
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path)
    run_manager.create_run("lit_run", "lab_experiment")
    return "lit_run"


PROFILE = profiles.DatasetProfile(
    name="batch", grouping="fly_ash",
    mass_balance_elements=("Ca",), starting_content_unit="wt%", solid_residue_unit="wt%")


# --------------------------------------------------------------------------- #
# Fake Anthropic client (one create() call → scripted response)
# --------------------------------------------------------------------------- #
def _resp_with_text(text):
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


class FakeClient:
    def __init__(self, text):
        self._text = text
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _resp_with_text(self._text)


def _payload(*candidates, note=""):
    import json
    return json.dumps({"candidates": list(candidates), "note": note})


def _cand(*, value=2.0, unit="wt%", quantity="calcite log Ksp", material="Class C fly ash",
          doi="10.1016/j.example.2020.01", url=None, quote="Calcite log Ksp is -8.48 at 25C.",
          title="A paper", year=2020, element=None, kind=None, matches=True, flags=None):
    cite = {"doi": doi, "url": url, "title": title, "authors": "Doe J", "year": year,
            "supporting_quote": quote}
    return {"value": value, "unit": unit, "quantity": quantity, "material": material,
            "element": element, "kind": kind,
            "conditions": {"temperature_C": 25, "pH": 7, "ionic_strength": "0.1 M"},
            "citation": cite,
            "conditions_match": {"matches": matches, "assessment": "ok",
                                 "mismatch_flags": flags or []},
            "confidence": 0.7}


# --------------------------------------------------------------------------- #
# Validation: uncited / quote-less dropped; DOI normalised; URL-only accepted
# --------------------------------------------------------------------------- #
def test_uncited_candidate_is_dropped():
    client = FakeClient(_payload(_cand(doi=None, url=None)))
    assert L.propose_literature_values({"quantity": "x"}, client=client) == []


def test_quoteless_candidate_is_dropped():
    client = FakeClient(_payload(_cand(quote="   ")))
    assert L.propose_literature_values({"quantity": "x"}, client=client) == []


def test_doi_is_normalised_to_doi_org_link():
    client = FakeClient(_payload(_cand(doi="https://doi.org/10.1016/j.example.2020.01")))
    out = L.propose_literature_values({"quantity": "x"}, client=client)
    assert len(out) == 1
    assert out[0].citation.doi == "10.1016/j.example.2020.01"
    assert out[0].source_link == "https://doi.org/10.1016/j.example.2020.01"


def test_url_only_candidate_is_accepted():
    client = FakeClient(_payload(_cand(doi=None, url="https://example.org/paper")))
    out = L.propose_literature_values({"quantity": "x"}, client=client)
    assert len(out) == 1
    assert out[0].source_link == "https://example.org/paper"


def test_quote_is_truncated_to_max_words():
    long_quote = " ".join(f"w{i}" for i in range(40))   # 40 words > MAX_QUOTE_WORDS
    client = FakeClient(_payload(_cand(quote=long_quote)))
    out = L.propose_literature_values({"quantity": "x"}, client=client)
    words = out[0].citation.supporting_quote.split()
    assert len(words) <= L.MAX_QUOTE_WORDS + 1          # +1 for the trailing ellipsis token
    assert words[-1] == "…"


def test_no_value_found_path_returns_empty():
    client = FakeClient(_payload(note="no reliable sourced value found"))
    assert L.propose_literature_values({"quantity": "x"}, client=client) == []


def test_disabled_without_key(monkeypatch):
    monkeypatch.delenv(import_assist.API_KEY_ENV, raising=False)
    # No client injected → resolves to None → empty, no exception.
    assert L.propose_literature_values({"quantity": "x"}) == []


# --------------------------------------------------------------------------- #
# Store: save (quarantined) → read → confirmed_records
# --------------------------------------------------------------------------- #
def test_save_quarantines_as_proposed_and_logs(run):
    client = FakeClient(_payload(_cand()))
    cands = L.propose_literature_values({"quantity": "x"}, client=client)
    added = L.save_candidates(run, cands)
    assert len(added) == 1
    recs = L.read_store(run)
    assert recs[0]["provenance"] == L.PROVENANCE_PROPOSED
    assert recs[0]["confirmed"] is False
    assert L.confirmed_records(run) == []                # nothing confirmed yet
    # A proposal audit event fired.
    events = audit.read_audit(run)
    assert (events["event_type"] == audit.EVENT_LITERATURE_PROPOSED).any()


# --------------------------------------------------------------------------- #
# Quarantine: an UNCONFIRMED literature value never reaches mass_balance
# --------------------------------------------------------------------------- #
def _assay_row():
    """A batch row with NO measured Ca starting assay (the literature stand-in case)."""
    return {"sample_id": "B1", "material_mass_g": 5.0, "liquid_volume_mL": 50.0,
            "solid_mass_g": 4.0, "Ca_mM": 20.0, "Ca_solid_residue": 0.4}


def test_unconfirmed_value_is_ignored_by_mass_balance(run):
    client = FakeClient(_payload(_cand(value=2.0, unit="wt%", element="Ca",
                                       kind="starting_assay", quantity="Ca content")))
    L.save_candidates(run, L.propose_literature_values(
        {"quantity": "Ca content", "kind": "starting_assay"}, client=client, kind="starting_assay"))

    row = _assay_row()
    # The gate injects ONLY confirmed values; unconfirmed → nothing injected.
    new_row, badges = L.row_with_confirmed_assays(row, L.read_store(run), PROFILE)
    assert "Ca_starting_content" not in new_row
    assert badges == {}
    # Therefore the closure is incomplete — the unconfirmed value did not enter it.
    res = mass_balance.closure(new_row, "Ca", profile=PROFILE)
    assert res["status"] == mass_balance.STATUS_INCOMPLETE
    assert "Ca_starting_content" in res["missing_fields"]


def test_confirmed_value_enters_mass_balance_with_a_source_badge(run):
    client = FakeClient(_payload(_cand(value=2.0, unit="wt%", element="Ca",
                                       kind="starting_assay", quantity="Ca content")))
    cands = L.propose_literature_values(
        {"quantity": "Ca content"}, client=client, kind="starting_assay")
    L.save_candidates(run, cands)
    L.confirm_value(run, cands[0].candidate_id)            # deliberate user confirm

    row = _assay_row()
    new_row, badges = L.row_with_confirmed_assays(row, L.read_store(run), PROFILE)
    assert new_row["Ca_starting_content"] == 2.0
    badge = badges["Ca_starting_content"]
    assert L.PROVENANCE_CONFIRMED in badge and "doi.org" in badge   # source shown, not "measured"
    # Now the closure completes off the confirmed stand-in (n_in = 100mg / M_Ca).
    res = mass_balance.closure(new_row, "Ca", profile=PROFILE)
    assert res["status"] == mass_balance.STATUS_COMPLETE
    assert res["n_in"] == pytest.approx(100.0 / units.MOLAR_MASSES["Ca"])


def test_confirmed_assay_never_overwrites_a_measured_value(run):
    client = FakeClient(_payload(_cand(value=9.9, unit="wt%", element="Ca",
                                       kind="starting_assay", quantity="Ca content")))
    cands = L.propose_literature_values({"quantity": "Ca content"}, client=client,
                                        kind="starting_assay")
    L.save_candidates(run, cands)
    L.confirm_value(run, cands[0].candidate_id)
    row = _assay_row()
    row["Ca_starting_content"] = 2.0                       # a real measured assay
    new_row, badges = L.row_with_confirmed_assays(row, L.read_store(run), PROFILE)
    assert new_row["Ca_starting_content"] == 2.0           # measured value untouched
    assert badges == {}


# --------------------------------------------------------------------------- #
# Conditions-mismatch double-acknowledgement gate
# --------------------------------------------------------------------------- #
def test_mismatch_requires_second_acknowledgement(run):
    client = FakeClient(_payload(_cand(matches=False, flags=["different temperature"])))
    cands = L.propose_literature_values({"quantity": "x"}, client=client)
    L.save_candidates(run, cands)
    cid = cands[0].candidate_id
    # Without the second acknowledgement → refused.
    with pytest.raises(L.ConditionsMismatchError):
        L.confirm_value(run, cid)
    assert L.confirmed_records(run) == []                  # still quarantined
    # With the acknowledgement → confirmed, and the ack is recorded.
    rec = L.confirm_value(run, cid, acknowledge_mismatch=True)
    assert rec["confirmed"] is True
    assert rec["acknowledged_mismatch"] is True


def test_confirm_logs_audit_with_doi_and_acknowledgement(run):
    client = FakeClient(_payload(_cand(matches=False, flags=["different material"],
                                       doi="10.1000/xyz")))
    cands = L.propose_literature_values({"quantity": "x"}, client=client)
    L.save_candidates(run, cands)
    L.confirm_value(run, cands[0].candidate_id, acknowledge_mismatch=True)

    events = audit.read_audit(run)
    confirmed = events[events["event_type"] == audit.EVENT_LITERATURE_CONFIRMED]
    assert len(confirmed) == 1
    payload = confirmed.iloc[0]["payload"]
    assert payload["citation_link"] == "https://doi.org/10.1000/xyz"   # traceable to the paper
    assert payload["conditions_mismatch"] is True
    assert payload["acknowledged_mismatch"] is True


def test_confirm_unknown_candidate_raises(run):
    with pytest.raises(L.LiteratureConfirmError):
        L.confirm_value(run, "does-not-exist")


# --------------------------------------------------------------------------- #
# DOI / link helpers
# --------------------------------------------------------------------------- #
def test_normalize_doi_strips_prefixes():
    for raw in ("10.1/x", "doi:10.1/x", "https://doi.org/10.1/x", "https://dx.doi.org/10.1/x"):
        assert L.normalize_doi(raw) == "10.1/x"
    assert L.normalize_doi(None) is None


def test_resolvable_link_prefers_doi():
    cite = {"doi": "10.1/x", "url": "https://example.org"}
    assert L.resolvable_link(cite) == "https://doi.org/10.1/x"
    assert L.resolvable_link({"doi": None, "url": "https://example.org"}) == "https://example.org"
