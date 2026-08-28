"""Phase 1B-R1 serialized ICP residual fail-closed regressions (synthetic only)."""
from __future__ import annotations

import pandas as pd
import pytest

from flyash_phreeqc_ml import calculations, profiles, replicates, report
from flyash_phreeqc_ml.compare import compare_measured_vs_phreeqc, comparison_inclusion
from flyash_phreeqc_ml.compare import inclusion as inclusion_contract
from flyash_phreeqc_ml.instruments import icp_processor as icp_qc
from flyash_phreeqc_ml.ml import residual_model, residual_stats
from flyash_phreeqc_ml.viz import compare_plots


def _serialized_ca_row(eligibility=...):
    row = {
        "sample_id": "S1",
        "leachant": "NaOH",
        "NaOH_M": 0.5,
        "CO2_condition": "OA",
        "time_min": 10,
        "liquid_solid_ratio": 5,
        "phreeqc_record_key": "pred-1",
        "Ca_mM": 2.0,
        "phreeqc_Ca_mM": 1.0,
        "residual_Ca": 1.0,
    }
    if eligibility is not ...:
        row["residual_Ca_validation_eligible"] = eligibility
    return row


@pytest.mark.parametrize(
    ("eligibility", "expected_state", "expected_code", "eligible"),
    [
        (True, icp_qc.SERIALIZED_RESIDUAL_ELIGIBLE, "", True),
        (False, icp_qc.SERIALIZED_RESIDUAL_INELIGIBLE,
         icp_qc.QC_SERIALIZED_ELIGIBILITY_FALSE, False),
        (..., icp_qc.SERIALIZED_RESIDUAL_LEGACY_UNKNOWN,
         icp_qc.QC_SERIALIZED_ELIGIBILITY_MISSING, False),
        ("not-a-qc-boolean", icp_qc.SERIALIZED_RESIDUAL_LEGACY_UNKNOWN,
         icp_qc.QC_SERIALIZED_ELIGIBILITY_MALFORMED, False),
    ],
)
def test_shared_serialized_residual_contract_is_explicit_and_fail_closed(
    eligibility, expected_state, expected_code, eligible,
):
    decision = icp_qc.serialized_residual_eligibility(
        _serialized_ca_row(eligibility), "Ca")
    assert decision.evidence_state == expected_state
    assert decision.qc_code == expected_code
    assert decision.validation_eligible is eligible


@pytest.mark.parametrize("eligibility", [False, ..., "not-a-qc-boolean"])
def test_statistics_exclude_false_missing_and_malformed_icp_qc(eligibility):
    comparison = pd.DataFrame([_serialized_ca_row(eligibility)])
    statuses = {"S1": replicates.MAPPING_STATUS_EXACT}
    assert residual_stats.bias_table(comparison, statuses, min_n=1).empty
    assert residual_stats.exact_residuals(comparison, statuses, "Ca").empty


def test_statistics_accept_explicit_true_when_existing_requirements_pass():
    comparison = pd.DataFrame([_serialized_ca_row(True)])
    statuses = {"S1": replicates.MAPPING_STATUS_EXACT}
    table = residual_stats.bias_table(comparison, statuses, min_n=1)
    pooled = table[
        (table["element"] == "Ca")
        & (table["condition_key"] == residual_stats.ALL_CONDITIONS)
    ].iloc[0]
    assert int(pooled["n_exact_pairs"]) == 1
    assert pooled["mean_residual"] == pytest.approx(1.0)


@pytest.mark.parametrize("eligibility", [False, ..., "not-a-qc-boolean"])
def test_residual_model_gate_rejects_forged_residual_without_explicit_true(eligibility):
    comparison = pd.DataFrame([_serialized_ca_row(eligibility)])
    gate = residual_model.gate_status(
        comparison, {"S1": replicates.MAPPING_STATUS_EXACT}, "Ca",
        min_pairs=1, min_conditions=1,
    )
    assert gate.n_exact_pairs == 0
    assert gate.meets is False


def test_residual_model_gate_retains_explicit_true_path():
    comparison = pd.DataFrame([_serialized_ca_row(True)])
    gate = residual_model.gate_status(
        comparison, {"S1": replicates.MAPPING_STATUS_EXACT}, "Ca",
        min_pairs=1, min_conditions=1,
    )
    assert gate.n_exact_pairs == 1
    assert gate.meets is True


@pytest.mark.parametrize("eligibility", [False, ..., "not-a-qc-boolean"])
def test_plots_exclude_icp_pair_without_explicit_true(eligibility):
    comparison = pd.DataFrame([_serialized_ca_row(eligibility)])
    mask = compare_plots._pair_mask(comparison, "Ca_mM", "phreeqc_Ca_mM")
    assert not mask.any()


def test_plots_retain_explicit_true_icp_pair():
    comparison = pd.DataFrame([_serialized_ca_row(True)])
    mask = compare_plots._pair_mask(comparison, "Ca_mM", "phreeqc_Ca_mM")
    assert mask.tolist() == [True]


def test_residual_bar_plot_does_not_leak_legacy_row_when_analyte_is_plottable(
    tmp_path, monkeypatch,
):
    from matplotlib.axes import Axes

    eligible = _serialized_ca_row(True)
    eligible["sample_id"] = "eligible"
    legacy = _serialized_ca_row(...)
    legacy.update({"sample_id": "legacy", "residual_Ca": 99.0, "Ca_mM": 100.0})
    comparison = pd.DataFrame([eligible, legacy])
    bar_heights = []
    original_bar = Axes.bar

    def recording_bar(self, x, height, *args, **kwargs):
        bar_heights.append(list(height))
        return original_bar(self, x, height, *args, **kwargs)

    monkeypatch.setattr(Axes, "bar", recording_bar)
    written = compare_plots.make_comparison_plots(comparison, tmp_path)
    assert {path.name for path in written} == {
        "measured_vs_phreeqc.png", "residuals_by_sample.png",
    }
    assert bar_heights == [[pytest.approx(1.0)]]


def test_inclusion_keeps_legacy_row_visible_with_actionable_unverified_reason():
    row = _serialized_ca_row(...)
    comparison = pd.DataFrame([row])
    measured = comparison[[
        "sample_id", "leachant", "NaOH_M", "CO2_condition", "time_min",
        "liquid_solid_ratio", "Ca_mM",
    ]].copy()
    mapping = pd.DataFrame([{
        "sample_id": "S1", "phreeqc_record_key": "pred-1",
    }])
    manifest = pd.DataFrame([{
        "phreeqc_record_key": "pred-1", "state": "batch",
        "liquid_solid_ratio": 5.0, "CO2_condition": "OA",
        "predicted_Ca_mM": 1.0,
    }])
    result = comparison_inclusion(
        measured, mapping, comparison, "Ca_mM", manifest=manifest,
        include_unsafe=True,
    )
    assert result["plotted"].empty
    assert len(result["excluded"]) == 1
    excluded = result["excluded"].iloc[0]
    assert excluded["reason"] == inclusion_contract.REASON_ICP_QC_UNVERIFIED
    assert "Re-run the comparison" in excluded["qc_reasons"]
    assert comparison.loc[0, "residual_Ca"] == pytest.approx(1.0)


def test_ph_only_statistics_and_plot_pair_do_not_require_icp_qc_evidence():
    comparison = pd.DataFrame([{
        "sample_id": "P1", "condition_key": "pH-condition",
        "final_pH": 12.5, "phreeqc_pH": 12.0, "residual_pH": 0.5,
    }])
    statuses = {"P1": replicates.MAPPING_STATUS_EXACT}
    table = residual_stats.bias_table(comparison, statuses, min_n=1)
    pooled = table[
        (table["element"] == "pH")
        & (table["condition_key"] == residual_stats.ALL_CONDITIONS)
    ].iloc[0]
    assert int(pooled["n_exact_pairs"]) == 1
    assert compare_plots._pair_mask(
        comparison, "final_pH", "phreeqc_pH").tolist() == [True]


def test_arithmetic_audit_labels_legacy_icp_but_leaves_ph_not_applicable():
    row = _serialized_ca_row(...)
    row.update({"final_pH": 12.5, "phreeqc_pH": 12.0, "residual_pH": 0.5})
    audit = calculations.audit_comparison(pd.DataFrame([row]))
    ca = audit[audit["formula"].str.startswith("residual_Ca")].iloc[0]
    ph = audit[audit["formula"].str.startswith("residual_pH")].iloc[0]
    assert ca["status"] == calculations.STATUS_PASS
    assert ca["serialized_icp_qc_state"] == icp_qc.SERIALIZED_RESIDUAL_LEGACY_UNKNOWN
    assert not bool(ca["validation_eligible"])
    assert "Re-run the comparison" in ca["qc_reason"]
    assert ph["status"] == calculations.STATUS_PASS
    assert ph["serialized_icp_qc_state"] == "not_applicable"
    assert pd.isna(ph["validation_eligible"])


def test_new_phase1b_comparison_serializes_true_and_flows_through_consumers():
    measured = pd.DataFrame([{
        "sample_id": "S1", "leachant": "NaOH", "NaOH_M": 0.5,
        "CO2_condition": "OA", "time_min": 10, "liquid_solid_ratio": 5,
        "Ca_mM": 2.0,
    }])
    predictions = pd.DataFrame([{
        "record_key": "pred-1", "state": "batch", "pH": 10.0,
        "mol_Ca": 0.001,
    }])
    comparison = compare_measured_vs_phreeqc(
        measured, predictions, mapping={"S1": "pred-1"})
    assert comparison.loc[0, "residual_Ca"] == pytest.approx(1.0)
    assert bool(comparison.loc[0, "residual_Ca_validation_eligible"])

    statuses = {"S1": replicates.MAPPING_STATUS_EXACT}
    assert not residual_stats.bias_table(comparison, statuses, min_n=1).empty
    assert compare_plots._pair_mask(
        comparison, "Ca_mM", "phreeqc_Ca_mM").tolist() == [True]
    assert residual_model.gate_status(
        comparison, statuses, "Ca", min_pairs=1, min_conditions=1).meets


def test_legacy_evidence_survives_report_export_as_unverified_state():
    comparison = pd.DataFrame([_serialized_ca_row(...)])
    exported = report._residuals_frame(comparison, profiles.FLY_ASH_PROFILE)
    assert exported.loc[0, "residual_Ca"] == pytest.approx(1.0)
    assert exported.loc[0, "residual_Ca_serialized_qc_state"] == (
        icp_qc.SERIALIZED_RESIDUAL_LEGACY_UNKNOWN)
    assert exported.loc[0, "residual_Ca_serialized_qc_code"] == (
        icp_qc.QC_SERIALIZED_ELIGIBILITY_MISSING)
    assert "Re-run the comparison" in exported.loc[
        0, "residual_Ca_serialized_qc_reason"]


def test_compare_ui_warns_for_visible_legacy_icp_residual():
    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    script = """
import pandas as pd
from ui.common import _render_legacy_icp_qc_warning
comparison = pd.DataFrame([{
    "sample_id": "S1", "Ca_mM": 2.0, "phreeqc_Ca_mM": 1.0,
    "residual_Ca": 1.0,
}])
_render_legacy_icp_qc_warning(comparison)
"""
    app = AppTest.from_string(script, default_timeout=60).run()
    assert not app.exception
    assert app.warning
    message = app.warning[0].value
    assert "QC unverified / legacy unknown" in message
    assert "Re-run the comparison" in message
    assert "remain visible" in message
