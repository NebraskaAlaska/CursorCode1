from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from flyash_phreeqc_ml.resources import (
    CatalogStore,
    Citation,
    KnowledgeFactType,
    KnowledgePack,
    KnowledgePackError,
    load_active_knowledge_context,
    RedistributionState,
    ResourceKind,
    ResourceManifest,
    SupportedSpeciesPhaseSummary,
    build_knowledge_pack,
    knowledge_pack_manifest,
    make_fact,
    scientific_resource_facts,
    write_knowledge_pack,
)


def _active_manifest(tmp_path: Path, kind: ResourceKind, marker: str) -> ResourceManifest:
    digest = marker * 64
    is_runtime = kind == ResourceKind.PHREEQC_RUNTIME
    resource_id = "wpi.phreeqc.runtime" if is_runtime else "wpi.phreeqc.database"
    path = tmp_path / ("phreeqc" if is_runtime else "phreeqc.dat")
    path.write_text(marker, encoding="utf-8")
    return ResourceManifest(
        resource_id=resource_id,
        installation_id=f"{resource_id}-3.8.6-{digest[:12]}",
        resource_kind=kind,
        display_name="PHREEQC runtime" if is_runtime else "phreeqc.dat",
        provider="USGS",
        installed_version="3.8.6-17100",
        executable_sha256=digest if is_runtime else "",
        database_sha256="" if is_runtime else digest,
        content_sha256=digest,
        install_path=str(path.resolve()),
        citation=Citation(
            title="PHREEQC official documentation",
            authors=("USGS",),
            url="https://water.usgs.gov/water-resources/software/PHREEQC/",
        ),
        rights_notice="USGS rights reviewed",
        redistribution_state=RedistributionState.PERMITTED,
        database_family="" if is_runtime else "phreeqc",
        supported_summary=(SupportedSpeciesPhaseSummary()
                           if is_runtime else SupportedSpeciesPhaseSummary(
                               master_species=("Ca",), solution_species=("Ca+2",),
                               phases=("Calcite", "Portlandite"))),
    )


def test_knowledge_pack_is_deterministic_offline_and_fact_types_are_distinct(
    tmp_path: Path,
) -> None:
    store = CatalogStore(tmp_path / "store")
    runtime = _active_manifest(tmp_path, ResourceKind.PHREEQC_RUNTIME, "a")
    database = _active_manifest(tmp_path, ResourceKind.PHREEQC_OFFICIAL_DATABASE, "b")
    for manifest in (runtime, database):
        store.register(manifest)
        store.activate(manifest.resource_id, manifest.installation_id)
    documentation = make_fact(
        KnowledgeFactType.DOCUMENTATION_STATEMENT,
        "The official manual describes PHREEQC input syntax.",
        citation=runtime.citation,
    )

    first = build_knowledge_pack(
        store,
        documentation_facts=(documentation,),
        project_policies=("Simulation is not experimental validation.",),
        model_inferences=("A future model may infer a compatibility risk.",),
    )
    second = build_knowledge_pack(
        store,
        documentation_facts=(documentation,),
        project_policies=("Simulation is not experimental validation.",),
        model_inferences=("A future model may infer a compatibility risk.",),
    )
    assert first.to_dict() == second.to_dict()
    assert {fact.fact_type for fact in first.facts} == set(KnowledgeFactType)
    runtime_fact = first.query(KnowledgeFactType.ACTIVE_RUNTIME_FACT)[0]
    assert first.provenance(runtime_fact.fact_id)["pack_hash"] == first.pack_hash
    assert runtime.installation_id in runtime_fact.source_installation_ids

    versioned = write_knowledge_pack(first, tmp_path / "offline-knowledge")
    assert versioned.is_file()
    assert write_knowledge_pack(first, tmp_path / "offline-knowledge") == versioned
    manifest = knowledge_pack_manifest(first, versioned)
    assert manifest.source_sha256 == first.pack_hash
    assert manifest.content_sha256 == hashlib.sha256(versioned.read_bytes()).hexdigest()
    assert manifest.resource_kind == ResourceKind.DOCUMENTATION_KNOWLEDGE_PACK


def test_knowledge_pack_rejects_hash_tampering(tmp_path: Path) -> None:
    store = CatalogStore(tmp_path / "store")
    runtime = _active_manifest(tmp_path, ResourceKind.PHREEQC_RUNTIME, "c")
    store.register(runtime)
    store.activate(runtime.resource_id, runtime.installation_id)
    document = build_knowledge_pack(store).to_dict()
    tampered = copy.deepcopy(document)
    tampered["facts"][0]["statement"] = "Invented replacement statement"
    with pytest.raises(KnowledgePackError, match="hash"):
        KnowledgePack.from_dict(tampered)


def _write_active_pack(tmp_path: Path) -> tuple[CatalogStore, KnowledgePack]:
    store = CatalogStore(tmp_path / "store")
    runtime = _active_manifest(tmp_path, ResourceKind.PHREEQC_RUNTIME, "d")
    database = _active_manifest(tmp_path, ResourceKind.PHREEQC_OFFICIAL_DATABASE, "e")
    for manifest in (runtime, database):
        store.register(manifest)
        store.activate(manifest.resource_id, manifest.installation_id)
    pack = build_knowledge_pack(store)
    versioned = write_knowledge_pack(pack, store.knowledge_dir)
    manifest = knowledge_pack_manifest(pack, versioned)
    projection_hash = store.load().knowledge_projection_hash
    full_hash = store.load().catalog_hash
    store.register_many((manifest,))
    store.bootstrap_activate(manifest.resource_id, manifest.installation_id)
    assert store.load().knowledge_projection_hash == projection_hash
    assert store.load().catalog_hash != full_hash
    return store, pack


def test_active_knowledge_loader_matches_catalog_and_exposes_exact_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, pack = _write_active_pack(tmp_path)
    context = load_active_knowledge_context(store.root)
    assert context.catalog_hash == store.load().knowledge_projection_hash == pack.catalog_hash
    assert context.pack_hash == pack.pack_hash
    assert {fact.fact_type for fact in context.facts} == {
        KnowledgeFactType.ACTIVE_RUNTIME_FACT,
        KnowledgeFactType.ACTIVE_DATABASE_FACT,
        KnowledgeFactType.DOCUMENTATION_STATEMENT,
        KnowledgeFactType.PROJECT_POLICY,
    }
    monkeypatch.setenv("WPI_RESOURCE_ROOT", str(store.root))
    payload = scientific_resource_facts()
    assert payload["available"] is True
    assert payload["pack_hash"] == pack.pack_hash
    assert all(item["fact_id"].startswith("fact-") for item in payload["facts"])
    assert all(
        item["source_installation_ids"]
        for item in payload["facts"]
        if item["fact_type"] in {
            KnowledgeFactType.ACTIVE_RUNTIME_FACT.value,
            KnowledgeFactType.ACTIVE_DATABASE_FACT.value,
        }
    )


def test_active_knowledge_loader_rejects_stale_pack(tmp_path: Path) -> None:
    store, _pack = _write_active_pack(tmp_path)
    extra = _active_manifest(tmp_path, ResourceKind.PHREEQC_RUNTIME, "f")
    store.register(extra)
    with pytest.raises(KnowledgePackError, match="stale"):
        load_active_knowledge_context(store.root)
    assert scientific_resource_facts(store.root) == {
        "available": False,
        "reason": "knowledge_pack_stale",
        "instruction": (
            "Do not claim a current PHREEQC runtime version, database identity, "
            "database contents, or resource hash from model memory."
        ),
    }


def test_agent_prompt_uses_versioned_resource_facts_and_saves_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from flyash_phreeqc_ml.agent.agent_prompts import build_user_prompt
    from flyash_phreeqc_ml.agent.agent_state import AgentState

    store, pack = _write_active_pack(tmp_path)
    monkeypatch.setenv("WPI_RESOURCE_ROOT", str(store.root))
    state = AgentState()
    prompt = build_user_prompt(state, "Which PHREEQC resources are active?")
    assert pack.pack_hash in prompt
    assert pack.catalog_projection in prompt
    assert store.load().knowledge_projection_hash in prompt
    for fact in pack.facts:
        assert fact.fact_id in prompt
        assert fact.statement in prompt
        assert f"fact_type={fact.fact_type.value}" in prompt
    provenance = state.to_provenance_dict()["resource_knowledge_provenance"]
    assert provenance["available"] is True
    assert provenance["pack_hash"] == pack.pack_hash


def test_agent_prompt_forbids_model_memory_when_pack_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from flyash_phreeqc_ml.agent.agent_prompts import build_user_prompt
    from flyash_phreeqc_ml.agent.agent_state import AgentState

    monkeypatch.delenv("WPI_RESOURCE_ROOT", raising=False)
    state = AgentState()
    prompt = build_user_prompt(state, "What database is active?")
    assert "resource_root_not_configured" in prompt
    assert "Do not claim a current PHREEQC runtime version" in prompt
    assert state.resource_knowledge_provenance["available"] is False


def test_phase5_static_interfaces_are_closed_and_safe() -> None:
    root = Path(__file__).parents[1] / "resources"
    schema_dir = root / "schemas"
    expected = {
        "resource_manifest.schema.json",
        "resource-update-request.schema.json",
        "update-proposal.schema.json",
        "test-evidence.schema.json",
        "promotion-request.schema.json",
        "rollback-request.schema.json",
        "resource-update-report.schema.json",
    }
    assert expected <= {path.name for path in schema_dir.glob("*.json")}
    for name in expected:
        schema = json.loads((schema_dir / name).read_text(encoding="utf-8"))
        assert schema["additionalProperties"] is False
        assert schema["$schema"].endswith("2020-12/schema")

    request = json.loads((root / "resource_update_request.json").read_text(encoding="utf-8"))
    report = json.loads((root / "resource_update_report.json").read_text(encoding="utf-8"))
    assert request["constraints"] == {
        "allow_database_switch": False,
        "allow_deploy": False,
        "allow_live_checkout_modification": False,
        "allow_main_modification": False,
        "requires_human_promotion": True,
        "sandbox_only": True,
    }
    assert report["active_installation_unchanged"] is True
