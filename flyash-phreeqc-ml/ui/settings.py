"""Settings section — AI configuration, PHREEQC engine status, app preferences (presentation only).

Lets the user configure the **AI assistant** (provider/model — never the key) and see the
**PHREEQC** executable/database status, toggle developer explanations, and read the intended
AI-framework / future-engine architecture. It owns no chemistry and no result-path logic; it
only reads status and sets per-session local AI provider/model choices and the dev-mode flag.

One of the seven top-level sections (Assistant · Workspace · Results · Data & Validation ·
Projects · Engine Library · Settings).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import streamlit as st

import app_ui
from flyash_phreeqc_ml.ai import client as ai_client
from flyash_phreeqc_ml.ai import config as ai_config
from flyash_phreeqc_ml.ai import provider_contract
from flyash_phreeqc_ml.security.identity import IdentityContext, current_identity
from flyash_phreeqc_ml.upload_guard import UploadValidationError, validate_upload
from flyash_phreeqc_ml.simulation import phreeqc_executor
from ui import state


OFFICIAL_CEMDATA_URL = "https://www.empa.ch/web/s308/thermodynamic-data"
OFFICIAL_PHREEQC_URL = "https://water.usgs.gov/water-resources/software/PHREEQC/"
OFFICIAL_PHREEQC_RIGHTS_URL = (
    "https://water.usgs.gov/water-resources/software/PHREEQC/Phreeqc_UserRightsNotice.txt"
)


def _short_path(path) -> str:
    if not path:
        return ""
    s = str(path)
    return s if len(s) <= 42 else "…" + s[-40:]


def _model_suggestions(provider: str, base_provider: str, base_model: str) -> list[str]:
    """Return provider-compatible suggestions without carrying a stale provider model.

    A configured Ollama model remains first only when the resolved configuration is
    already Ollama.  Switching from Anthropic/disabled to Ollama starts from an
    official Ollama tag instead of presenting a Claude model as a local suggestion.
    """
    if provider == ai_config.PROVIDER_OLLAMA:
        configured = [base_model] if base_provider == provider and base_model else []
        return list(dict.fromkeys([*configured, *ai_config.OLLAMA_SUGGESTED_MODELS]))
    return list(dict.fromkeys([base_model, *ai_config.SUGGESTED_MODELS]))


def _render_ai_settings(identity: IdentityContext | None = None) -> None:
    """AI provider/model selector + status + the master **Enable live AI assistant** toggle.

    Never shows the key. Local choices live only in this Streamlit session. Hosted
    choices are deployment-admin configuration and ordinary users receive a read-only
    view; no process-global override or browser-supplied endpoint is trusted."""
    principal = identity or current_identity()
    app_ui.section_header("AI assistant",
                          "conversation / planning / explanation — never the chemistry")
    base = ai_config.resolve_config()

    if principal.is_hosted:
        cfg = base
        st.info("Hosted AI configuration is administrator-managed. Users can select only the "
                "approved service/model shown here; the private endpoint and credentials are "
                "not exposed in the browser.")
    else:
        provider_options = list(ai_config.SUPPORTED_PROVIDERS)
        current_provider = base.provider if base.provider in provider_options else 0
        provider = st.selectbox(
            "Provider", provider_options,
            index=provider_options.index(current_provider) if current_provider in provider_options else 0,
            key="ai_provider_choice",
            format_func=lambda value: {
                ai_config.PROVIDER_DISABLED: "AI disabled",
                ai_config.PROVIDER_ANTHROPIC: "Anthropic cloud",
                ai_config.PROVIDER_OLLAMA: "Ollama (native API)",
                ai_config.PROVIDER_OPENAI_COMPATIBLE: "OpenAI-compatible endpoint",
                ai_config.PROVIDER_HOSTED: "Administrator-hosted endpoint",
            }.get(value, value),
        )
        default_location = {
            ai_config.PROVIDER_DISABLED: "disabled",
            ai_config.PROVIDER_ANTHROPIC: "cloud",
            ai_config.PROVIDER_HOSTED: "server",
        }.get(provider, st.session_state.get(ai_config.SESSION_LOCATION_KEY, "local"))
        location_options = (["disabled"] if provider == ai_config.PROVIDER_DISABLED else
                            ["cloud"] if provider == ai_config.PROVIDER_ANTHROPIC else
                            ["server"] if provider == ai_config.PROVIDER_HOSTED else
                            ["local", "server", "cloud"])
        location = st.selectbox(
            "Provider classification", location_options,
            index=(location_options.index(default_location)
                   if default_location in location_options else 0),
            key="ai_location_choice",
        )
        base_url = None
        if provider in {ai_config.PROVIDER_OLLAMA, ai_config.PROVIDER_OPENAI_COMPATIBLE,
                        ai_config.PROVIDER_HOSTED}:
            default_url = (st.session_state.get(ai_config.SESSION_BASE_URL_KEY)
                           or ("http://127.0.0.1:11434" if provider == ai_config.PROVIDER_OLLAMA
                               else "http://127.0.0.1:8000/v1"))
            base_url = st.text_input(
                "Base URL", value=default_url, key="ai_base_url_choice",
                help="HTTP(S) only. Unsafe, credential-bearing, metadata, or wrongly classified "
                     "addresses are rejected before any request.",
            ).strip()
        base_model = ai_config.resolve_model()
        options = _model_suggestions(provider, base.provider, base_model)
        picked = st.selectbox("Model (suggested)", options, index=0, key="ai_model_pick",
                              help="Used only for explicitly enabled AI suggestions.")
        custom = st.text_input("…or enter a model id", key="ai_model_custom",
                               help="Leave blank to use the selected model above.").strip()
        chosen_model = custom or picked
        # Plain session keys are the sole UI authority. They are per-session and
        # survive widget garbage collection when leaving Settings.
        st.session_state[ai_config.SESSION_PROVIDER_KEY] = provider
        st.session_state[ai_config.SESSION_MODEL_KEY] = chosen_model
        st.session_state[ai_config.SESSION_LOCATION_KEY] = location
        if base_url:
            st.session_state[ai_config.SESSION_BASE_URL_KEY] = base_url
        else:
            st.session_state.pop(ai_config.SESSION_BASE_URL_KEY, None)
        cfg = ai_config.resolve_config(provider=provider, model=chosen_model,
                                       base_url=base_url, location=location)

    # Capability (key + SDK) vs. the user's master switch. ``cfg.enabled`` is exactly
    # "key present AND SDK present"; the toggle is only operable when that holds.
    #
    # The choice is persisted in a PLAIN session key (``state.LIVE_AI_KEY``) that the Assistant
    # reads. It must NOT be the toggle's own widget key: a widget-keyed value is dropped by
    # Streamlit the moment the widget stops rendering (i.e. as soon as you leave Settings), which
    # would silently reset live AI on the next rerun. So the toggle uses its own widget key and
    # syncs into the plain key on change; the plain key survives navigation + prompt submission.
    st.session_state.setdefault(state.LIVE_AI_KEY, False)
    if state.LIVE_AI_WIDGET_KEY not in st.session_state:        # re-seed after widget GC
        st.session_state[state.LIVE_AI_WIDGET_KEY] = bool(st.session_state[state.LIVE_AI_KEY])
    toggle_on = bool(st.session_state[state.LIVE_AI_KEY])
    # The ONE shared live-AI status (same helper the Assistant reads → they can never disagree).
    ai_status = ai_config.live_ai_status(cfg, toggle_on)
    live_on = ai_status.active

    # Status card: API key · AI SDK · Live AI · provider/model (all key-free).
    credential_needed = cfg.provider == ai_config.PROVIDER_ANTHROPIC \
        or (cfg.provider == ai_config.PROVIDER_OPENAI_COMPATIBLE and cfg.location == "cloud")
    app_ui.render_metric_cards([
        {"label": "Credential", "value": ("Detected" if cfg.key_present else "Missing")
         if credential_needed else "Not required",
         "caption": (cfg.key_source if cfg.key_present else
                     f"set {ai_config.API_KEY_ENV}" if cfg.provider == ai_config.PROVIDER_ANTHROPIC
                     else "never displayed"),
         "status": "success" if (cfg.key_present or not credential_needed) else "neutral"},
        {"label": "Provider adapter", "value": "Available" if cfg.sdk_available else "Missing",
         "caption": cfg.location,
         "status": "success" if cfg.sdk_available else "neutral"},
        {"label": "Live AI", "value": "Enabled" if live_on else "Disabled",
         "status": "success" if live_on else "neutral"},
        {"label": "Model", "value": cfg.model, "caption": cfg.provider},
    ])

    # The master enable switch — operable only when a key + the SDK are present. ``on_change``
    # mirrors the widget into the persistent plain key so the choice survives navigation/reruns.
    def _sync_live_ai() -> None:
        st.session_state[state.LIVE_AI_KEY] = bool(
            st.session_state.get(state.LIVE_AI_WIDGET_KEY, False))

    st.toggle("Enable live AI assistant", key=state.LIVE_AI_WIDGET_KEY, on_change=_sync_live_ai,
              disabled=not cfg.enabled,
              help="When on, the assistant sends your conversation to the API to phrase and plan "
                   "the next step. It never runs PHREEQC, saves, or touches the science without "
                   "your explicit confirmation.")
    if not cfg.enabled:
        st.caption(f"⚪ Can't enable live AI yet — {cfg.disabled_reason()}. The assistant still "
                   "works fully with the deterministic planner (it asks, plans, previews, and "
                   "runs on your confirmation).")
    elif live_on:
        st.success("🟢 Live AI is **on** for the assistant — phrasing, planning, and explanation "
                   "only. It never runs PHREEQC or saves anything without your confirmation, and "
                   "never affects mapping / residuals / validation.")
    else:
        st.caption("⚪ Live AI is **off** — the assistant uses the deterministic planner. Turn the "
                   "toggle on to use AI phrasing/planning (this sends conversation data to the API "
                   "for the assistant only — data leaves this machine).")

    st.caption(f"Role: {ai_config.AI_ROLE_LINE}.")
    st.caption(cfg.privacy_notice)
    st.caption("Credentials are administrator/environment secrets only; they are never entered, "
               "shown, logged, exported, or copied into session state.")
    if cfg.provider == ai_config.PROVIDER_ANTHROPIC and not cfg.key_present:
        st.caption(f"To configure Anthropic, set `{ai_config.API_KEY_ENV}` as an environment or "
                   "Streamlit secret; never enter it in the browser.")
    st.warning(ai_config.AI_EXPERIMENTAL_WARNING)

    _render_ai_diagnostics(cfg, toggle_on, principal)


def _render_ai_diagnostics(cfg, toggle_on: bool,
                           identity: IdentityContext | None = None) -> None:
    """Safe diagnostics + a one-click live smoke test (no key, no raw response ever shown).

    Helps debug 'live AI is unavailable' without exposing secrets: it reports credential
    *presence* (never its value/length), adapter availability, the selected model, and runs a harmless one-line
    prompt through the **same** client the assistant uses, surfacing only a sanitized category."""
    with app_ui.advanced_expander("AI diagnostics (safe — no key shown)"):
        diag = ai_config.diagnostics(toggle_on=toggle_on)
        safe = diag.to_safe_dict()
        # Key length is retained in the compatibility API but intentionally omitted
        # from the hosted/user-visible diagnostics surface.
        safe.pop("key_length", None)
        st.write(safe)
        st.caption("Only provider classification and credential presence are shown; endpoint "
                   "addresses and secret values are hidden in hosted mode.")
        st.markdown("**Live AI smoke test** — send a harmless one-sentence prompt through the same "
                    "client the assistant uses. It never logs the key or stores the response.")
        if st.button("Run live AI smoke test", key="ai_smoke_test", disabled=not cfg.enabled,
                     help=None if cfg.enabled else f"Needs a key + SDK — {cfg.disabled_reason()}."):
            with st.spinner("Sending a one-sentence test prompt…"):
                res = ai_client.smoke_test()
            if res.ok:
                st.success(f"✅ Live AI reachable (model `{res.model}`) — key, SDK, and network all "
                           "work. The assistant can use live AI.")
            else:
                st.error(f"❌ Live AI call failed — **{res.category}**: {res.message} "
                         f"(model `{res.model}`). The assistant falls back to the deterministic "
                         "planner; the toggle stays on.")

        if cfg.capabilities.model_discovery:
            if st.button("Discover installed models", key="ai_discover_models",
                         disabled=not cfg.enabled):
                try:
                    settings = ai_config.resolve_provider_settings()
                    secret, _ = ai_config.detect_api_key(cfg.provider)
                    adapter = provider_contract.create_adapter(settings, secret=secret)
                    discovered = adapter.discover_models()
                    st.session_state["ai_discovered_models"] = [
                        item.to_safe_dict() for item in discovered]
                except Exception as exc:  # controlled diagnostic; never raw response text
                    st.error(f"Model discovery unavailable ({type(exc).__name__}).")
            models = st.session_state.get("ai_discovered_models", [])
            if models:
                st.write(models)

        if cfg.provider == ai_config.PROVIDER_OLLAMA and cfg.location == "local" \
                and not (identity and identity.is_hosted):
            st.markdown("**Explicit local model pull**")
            st.caption("No model is downloaded at startup or during discovery. Review the model "
                       "name/size first, then type the exact confirmation.")
            pull_model = st.text_input("Model to pull", key="ai_pull_model").strip()
            confirmation = st.text_input(
                f"Type PULL {pull_model or '<model-id>'}", key="ai_pull_confirmation").strip()
            if st.button("Pull confirmed model", key="ai_pull_confirmed",
                         disabled=not pull_model or confirmation != f"PULL {pull_model}"):
                try:
                    settings = ai_config.resolve_provider_settings()
                    adapter = provider_contract.create_adapter(settings)
                    result = adapter.pull_model(pull_model, confirmation=confirmation)
                    st.success(result.message or "Model pull completed.")
                except Exception as exc:
                    st.error(f"Model pull unavailable ({type(exc).__name__}).")


def _render_phreeqc_engine() -> None:
    app_ui.section_header("Geochemical engine — PHREEQC",
                          "the first executable engine (leaching / aqueous dissolution)")
    av = phreeqc_executor.check_availability()
    app_ui.render_metric_cards([
        {"label": "PHREEQC", "value": "Ready" if av.can_run else "Not configured",
         "status": "success" if av.can_run else "warning"},
        {"label": "Executable", "value": "Found" if av.executable_found else "Missing",
         "caption": _short_path(av.executable_path) or "set PHREEQC_EXE",
         "status": "success" if av.executable_found else "neutral"},
        {"label": "Database", "value": "Found" if av.database_found else "Missing",
         "caption": _short_path(av.database_path) or "set PHREEQC_DATABASE",
         "status": "success" if av.database_found else "neutral"},
    ])
    st.caption(phreeqc_executor.availability_hint(av))
    st.caption("Runtime/database provider: U.S. Geological Survey (USGS), PHREEQC 3. "
               "The canonical release preserves the complete upstream User Rights Notice at "
               "`/opt/phreeqc/share/doc/phreeqc/USGS_USER_RIGHTS_NOTICE.txt` and "
               "`release/PHREEQC_USGS_RIGHTS_NOTICE.txt`; publications/products must "
               "appropriately acknowledge the authors and USGS as required by that notice.")
    st.link_button("USGS PHREEQC source and documentation", OFFICIAL_PHREEQC_URL)
    st.link_button("USGS PHREEQC User Rights Notice", OFFICIAL_PHREEQC_RIGHTS_URL)
    st.caption("Configure by pointing the app at a PHREEQC CLI you supply: set `PHREEQC_EXE` and "
               "`PHREEQC_DATABASE`. Explicit redistribution permission for CEMDATA18 was not "
               "found in the reviewed official evidence, so it is external-only and must be "
               "supplied by a user or administrator; it is never bundled. The "
               "assistant still **plans** and builds reviewable input without it.")
    st.caption("**Hosted deployment:** to run PHREEQC server-side so colleagues need only a "
               "browser, see `docs/deployment.md` — a Docker image with PHREEQC built in, where "
               "`PHREEQC_EXE` / `PHREEQC_DATABASE` / `ANTHROPIC_API_KEY` are server-side environment "
               "variables / secrets (never entered or shown in the browser).")


def _render_database_manager(identity: IdentityContext | None = None) -> None:
    """Thin, defensive UI adapter over the scientific resource package.

    It shows the exact currently resolved runtime/database identity. Import delegates
    parsing, rights enforcement, side-by-side installation, and catalog registration to
    ``resources.ExternalDatabaseImporter``; this UI never merges/concatenates databases
    and never activates a newly imported candidate.
    """
    principal = identity or current_identity()
    app_ui.section_header("Database Manager",
                          "verified identity · rights-aware import · no silent activation")
    availability = phreeqc_executor.check_availability()
    environment = availability.environment_identity
    if environment is None:
        st.warning("No executable/database pair has a verified active identity. Preview remains "
                   "available, but execution is unavailable.")
    else:
        st.json({
            "environment_identity_hash": environment.identity_hash,
            "executable_sha256": environment.executable.sha256,
            "executable_size_bytes": environment.executable.size_bytes,
            "database_sha256": environment.database.sha256,
            "database_size_bytes": environment.database.size_bytes,
            "phreeqc_version": os.environ.get("PHREEQC_VERSION") or "not recorded",
            "database_resource_id": os.environ.get("PHREEQC_DATABASE_ID") or "not recorded",
            "database_version": os.environ.get("PHREEQC_DATABASE_VERSION") or "not recorded",
        })
        st.caption("Hashes identify the exact active files. A database/runtime change after review "
                   "requires a fresh preview and confirmation.")

    st.caption("Provider and attribution: U.S. Geological Survey (USGS), PHREEQC 3. The full "
               "User Rights Notice is preserved in the runtime image at "
               "`/opt/phreeqc/share/doc/phreeqc/USGS_USER_RIGHTS_NOTICE.txt`. The notice requires "
               "appropriate acknowledgment of the authors and USGS.")
    st.link_button("USGS PHREEQC release page", OFFICIAL_PHREEQC_URL)
    st.link_button("USGS PHREEQC User Rights Notice", OFFICIAL_PHREEQC_RIGHTS_URL)

    st.link_button("Official Empa CEMDATA source", OFFICIAL_CEMDATA_URL)
    st.caption("CEMDATA18.11 is an external scientific resource. Review its official archive, "
               "rights/licence evidence, version, and citation before importing. Import does not "
               "activate it and extensions are never concatenated with a base database.")

    may_import = not principal.is_hosted or principal.is_admin
    if not may_import:
        st.info("Hosted database resources are read-only for ordinary users. An administrator "
                "must review, import, verify, test, and separately activate resources.")
        return

    with app_ui.advanced_expander("Import one reviewed local database candidate"):
        st.warning("Import creates a side-by-side review candidate only. It does not select the "
                   "database for a run or change the active database.")
        upload = st.file_uploader("PHREEQC database (.dat, .zip, .tar, .tar.gz)",
                                  type=["dat", "zip", "tar", "tar.gz", "tgz"],
                                  key="database_manager_upload")
        validated = None
        archive_member = None
        if upload is not None:
            try:
                validated = validate_upload(
                    upload, allowed_extensions={".dat", ".zip", ".tar", ".tar.gz", ".tgz"})
                if validated.extension != ".dat":
                    from flyash_phreeqc_ml.resources.sources import archive_database_members
                    with tempfile.TemporaryDirectory(prefix="vl-db-review-") as temporary_dir:
                        temporary_path = Path(temporary_dir) / validated.filename
                        temporary_path.write_bytes(validated.data)
                        candidates = archive_database_members(temporary_path)
                    if not candidates:
                        st.error("The reviewed archive contains no regular .dat member.")
                        validated = None
                    elif len(candidates) == 1:
                        archive_member = candidates[0]
                        st.caption(f"Archive database member: `{archive_member}`")
                    else:
                        archive_member = st.selectbox(
                            "Exact .dat archive member to import (one file only)", candidates,
                            key="database_manager_archive_member")
                        st.caption("Only the selected verified member is read; the archive is never "
                                   "extracted or concatenated.")
            except UploadValidationError as exc:
                st.error(f"Upload refused ({exc.code}): {exc}")
                validated = None
            except Exception as exc:
                st.error(f"Archive inventory refused ({type(exc).__name__}).")
                validated = None
        resource_id = st.text_input("Resource ID", value="external.phreeqc.database",
                                    key="database_manager_resource_id").strip()
        display_name = st.text_input("Display name", key="database_manager_display_name").strip()
        installed_version = st.text_input("Version", key="database_manager_version").strip()
        rights_notice = st.text_area(
            "Rights/licence evidence", key="database_manager_rights_notice",
            help="Record the exact source/location and what permits this local/server use.").strip()
        rights_confirmed = st.checkbox(
            "I reviewed the source and confirm I have rights to install/use this exact file",
            key="database_manager_rights_confirmed")
        redistribution = st.selectbox(
            "Redistribution state",
            ["user_supplied", "external_only", "permitted", "unknown"],
            key="database_manager_redistribution")
        ready = bool(validated and resource_id and display_name and installed_version
                     and rights_notice and rights_confirmed)
        if st.button("Import as review candidate", key="database_manager_import",
                     disabled=not ready):
            try:
                from flyash_phreeqc_ml import config as app_config
                from flyash_phreeqc_ml.resources import (
                    CatalogStore, ExternalDatabaseImporter, RedistributionState)

                store_root = Path(os.environ.get(
                    "WPI_RESOURCE_ROOT", str(app_config.OUTPUTS_DIR / "resources")))
                importer = ExternalDatabaseImporter(CatalogStore(store_root))
                with tempfile.TemporaryDirectory(prefix="vl-db-import-") as temporary_dir:
                    temporary_path = Path(temporary_dir) / validated.filename
                    temporary_path.write_bytes(validated.data)
                    manifest = importer.import_database(
                        temporary_path,
                        resource_id=resource_id,
                        display_name=display_name,
                        provider="user-supplied",
                        installed_version=installed_version,
                        rights_confirmed=rights_confirmed,
                        rights_notice=rights_notice,
                        redistribution_state=RedistributionState(redistribution),
                        actor_role="admin" if principal.is_admin else "user",
                        deployment_scope="hosted" if principal.is_hosted else "local",
                        archive_member=archive_member,
                        official_source_url="",
                    )
                st.success(f"Imported candidate `{manifest.installation_id}`. It is not active.")
            except UploadValidationError as exc:
                st.error(f"Upload refused ({exc.code}): {exc}")
            except Exception as exc:
                # Resource errors contain controlled policy/validation text. The
                # uploaded contents and traceback are never rendered.
                st.error(f"Database import refused ({type(exc).__name__}). Review the metadata, "
                         "rights evidence, and resource-store configuration.")


def _render_resource_steward(identity: IdentityContext | None = None) -> None:
    """Render a read-only view of deterministic update evidence.

    Discovery, download, build, test, promotion, finding resolution, and rollback remain
    command-line/automation operations with their closed role and confirmation contracts.  The
    browser is deliberately unable to mutate the resource catalog or a proposal.
    """
    principal = identity or current_identity()
    app_ui.section_header(
        "Runtime & Database Steward",
        "official-source proposals · quarantined candidates · explicit human promotion",
    )
    st.caption(
        "Read-only evidence view. Opening this page never checks the network, downloads a "
        "candidate, changes an active pointer, promotes, or rolls back a resource."
    )

    try:
        from flyash_phreeqc_ml import config as app_config
        from flyash_phreeqc_ml.resources import CatalogStore, ResourceSteward

        store_root = Path(os.environ.get(
            "WPI_RESOURCE_ROOT", str(app_config.OUTPUTS_DIR / "resources")))
        store = CatalogStore(store_root)
        catalog = store.load()
        proposals = ResourceSteward(store).show()
    except Exception as exc:
        # Paths, proposal payloads, uploaded content, and tracebacks are intentionally omitted.
        st.error(
            f"Steward evidence is unavailable ({type(exc).__name__}). The active resource was "
            "not changed. An administrator must inspect the resource store."
        )
        return

    app_ui.render_metric_cards([
        {"label": "Catalog generation", "value": catalog.generation},
        {"label": "Installations", "value": len(catalog.resources)},
        {"label": "Active pointers", "value": len(catalog.active)},
        {"label": "Update proposals", "value": len(proposals)},
    ])
    st.caption(
        "Installed versions remain side by side. Durable runs retain their exact runtime and "
        "database installation identities even after a later explicit activation."
    )

    if catalog.active:
        active_by_id = {item.installation_id: item for item in catalog.resources}
        active_rows = []
        for resource_id, installation_id in sorted(catalog.active.items()):
            manifest = active_by_id.get(installation_id)
            active_rows.append({
                "resource_id": resource_id,
                "installation_id": installation_id,
                "version": manifest.installed_version if manifest else "unresolved",
                "sha256": manifest.primary_sha256 if manifest else "unresolved",
                "rollback_state": (manifest.rollback_state.value
                                   if manifest else "unresolved"),
            })
        with app_ui.advanced_expander("Active managed resource identities"):
            st.dataframe(active_rows, use_container_width=True, hide_index=True)
    else:
        st.info(
            "No active managed-resource pointers are recorded in this store. Runtime environment "
            "variables may still identify a release bootstrap, but no update is inferred here."
        )

    if not proposals:
        st.info(
            "Update status: no Steward proposal is recorded. This is not evidence that a newer "
            "official release exists or does not exist; run the official-source update monitor "
            "and Steward check to create reviewable evidence."
        )
        st.caption(
            "Rollback status: unavailable because no proposal has been explicitly promoted. "
            "Rollback is never automatic."
        )
        return

    proposal_ids = [item.proposal_id for item in proposals]
    selected_id = st.selectbox(
        "Candidate update report",
        proposal_ids,
        index=len(proposal_ids) - 1,
        key="resource_steward_proposal",
        format_func=lambda proposal_id: next(
            f"{item.resource_id} · {item.candidate_version} · {item.status.value}"
            for item in proposals if item.proposal_id == proposal_id
        ),
    )
    proposal = next(item for item in proposals if item.proposal_id == selected_id)
    by_installation = {item.installation_id: item for item in catalog.resources}
    current = by_installation.get(proposal.current_installation_id)
    current_version = current.installed_version if current else "not recorded"
    version_differs = bool(current and current.installed_version != proposal.candidate_version)
    if proposal.status.value == "rejected":
        update_label = "Rejected candidate"
        update_status = "warning"
    elif version_differs:
        update_label = "Candidate update available"
        update_status = "success"
    elif current:
        update_label = "Same-version candidate review"
        update_status = "warning"
    else:
        update_label = "Candidate proposal recorded"
        update_status = "neutral"
    app_ui.render_metric_cards([
        {"label": "Update status", "value": update_label, "status": update_status},
        {"label": "Active version at check", "value": current_version},
        {"label": "Candidate version", "value": proposal.candidate_version},
        {"label": "Steward stage", "value": proposal.status.value},
    ])
    st.caption(
        "‘Candidate update available’ means only that a version-different official-source "
        "proposal exists in this local Steward store. It is not a claim that the candidate is "
        "safe, compatible, tested, approved, active, or experimentally validated."
    )

    report = {
        "proposal_id": proposal.proposal_id,
        "resource_id": proposal.resource_id,
        "resource_kind": proposal.resource_kind.value,
        "status": proposal.status.value,
        "candidate_version": proposal.candidate_version,
        "official_source_url": proposal.source_url,
        "archive_filename": proposal.archive_filename,
        "expected_source_sha256": proposal.expected_source_sha256 or "not recorded",
        "observed_source_sha256": proposal.observed_source_sha256 or "not verified",
        "candidate_sha256": proposal.candidate_sha256 or "not built",
        "redistribution_state": proposal.redistribution_state.value,
        "quarantined": bool(proposal.quarantine_path),
        "current_installation_id": proposal.current_installation_id or "none",
        "candidate_installation_id": proposal.candidate_installation_id or "not built",
        "unresolved_high_or_medium_findings": len(proposal.unresolved_blockers),
    }
    with app_ui.advanced_expander("Candidate identity and rights report"):
        st.json(report)
        st.link_button("Open the recorded official source", proposal.source_url)
        st.caption(
            "The quarantine filesystem path and any raw worker output are intentionally hidden "
            "from the browser. Rights evidence must pass before build/test/promotion."
        )

    evidence_rows = [
        {
            "stage": stage,
            "evidence_id": item.evidence_id,
            "status": item.status.value,
            "artifact_sha256": item.artifact_sha256 or "not recorded",
            "summary": item.summary,
        }
        for stage, values in (
            ("build", proposal.build_evidence), ("test", proposal.test_evidence))
        for item in values
    ]
    if evidence_rows:
        with app_ui.advanced_expander("Deterministic build and test evidence"):
            st.dataframe(evidence_rows, use_container_width=True, hide_index=True)
    else:
        st.warning(
            "Candidate evidence is incomplete: no deterministic build/test evidence is recorded. "
            "Promotion remains blocked."
        )

    if proposal.comparison is not None:
        with app_ui.advanced_expander("Scientific compatibility comparison"):
            st.json(proposal.comparison.to_dict())
            st.caption(
                "Removed parsed species/phases and every unresolved high/medium finding block "
                "promotion. A comparison is advisory software evidence, not experimental "
                "validation."
            )
    if proposal.findings:
        with app_ui.advanced_expander("Steward findings"):
            st.dataframe(
                [item.to_dict() for item in proposal.findings],
                use_container_width=True,
                hide_index=True,
            )

    if proposal.status.value == "promoted" and proposal.previous_active_installation_id:
        st.warning(
            "Rollback status: available to a human administrator using the exact proposal and "
            "candidate hash confirmation. This browser view cannot execute it."
        )
    elif proposal.status.value == "rolled_back":
        st.success(
            "Rollback status: completed and recorded. The prior installation identity was "
            "restored; the candidate remains side by side for auditability."
        )
    else:
        st.caption(
            "Rollback status: unavailable until a completely tested candidate is explicitly "
            "promoted by a human administrator. No rollback has been executed."
        )
    if principal.is_hosted:
        st.caption(
            "Hosted policy: ordinary website users have read-only evidence access. Deployment "
            "administrators perform candidate operations outside the browser."
        )


def _render_preferences() -> None:
    app_ui.section_header("Preferences")
    st.checkbox("🛠️ Developer explanation mode", value=False, key="dev_mode",
                help="Show deeper chemistry/statistics explanations (mainly in Data & Validation).")


def _render_future_architecture() -> None:
    app_ui.section_header("AI framework & future engines",
                          "designed for, not yet built — no LangGraph dependency added")
    st.markdown(
        "**Current:** a custom AI agent layer → tool/action registry → policy gate → deterministic "
        "backend tools, with **human confirmation before any execution**.\n\n"
        "**Future (compatible by design):**\n"
        "- **LangGraph-style stateful orchestrator** — the propose → policy-gate → confirm → "
        "deterministic-tool loop is already a state machine (`AgentState` = graph state, actions = "
        "nodes, the policy = the edge function), so it can be re-expressed as a graph without "
        "changing the safety model.\n"
        "- **Plugin engine registry** — engines register per domain (today only "
        "`leaching_geochemistry → PHREEQC`); see **Engine Library**.\n"
        "- **Literature / RAG agent**, **ML / surrogate agent**, **simulation-engine agents** "
        "(atomistic / mechanical-property / thermal), and a **validation / calibration agent**.\n\n"
        "No LangGraph dependency is added yet. See `docs/ai_architecture.md`.")


def _render_settings(selected_run: str | None, *,
                     identity: IdentityContext | None = None) -> None:
    app_ui.render_page_header(
        "Settings",
        "Configure the AI assistant and the PHREEQC engine, set preferences, and read the "
        "intended AI-framework architecture.",
        eyebrow="AI · engine · preferences · architecture")
    _render_ai_settings(identity)
    st.divider()
    _render_phreeqc_engine()
    st.divider()
    _render_database_manager(identity)
    st.divider()
    _render_resource_steward(identity)
    st.divider()
    _render_preferences()
    st.divider()
    _render_future_architecture()


# The app dispatches to ``render``.
render = _render_settings
