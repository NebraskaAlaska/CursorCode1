# NSF demonstration script

This is a 5–10 minute **SYNTHETIC DEMO** route. It demonstrates a reproducible software workflow and scientific safety gates. It is not measured WPI research, experimental validation, a production-model prediction, or evidence that the synthetic material exists.

Before the meeting run `./scripts/run_nsf_demo.sh --build` (Windows: `scripts/run_nsf_demo.ps1 -Build`) and complete `NSF_DEMO_ACCEPTANCE_CHECKLIST.md` against the exact image. The preflight and upstream example suite establish software operability only.

## Prepared synthetic records

Create a project named `SYNTHETIC DEMO — NSF walkthrough` and a material named `SYNTHETIC DEMO fly ash`. Record composition provenance as `user_assumption`, verification as `unverified`, and describe every entered value as synthetic workflow input. Never choose `measured` for these records. Use only the header-only ICP template or the prefilled Phase 3 synthetic rows; do not import research data.

## Speaking route

1. **Home and durable context — 45 seconds.** Point to the active project/material IDs and the `SYNTHETIC DEMO` names. Say: “Measured, simulated, inferred, advisory, literature-derived, synthetic-demo, and ML-predicted records stay distinct.” Open the material record briefly to show provenance, assumptions, unresolved issues, and the `unverified` state.

2. **PHREEQC identity and preview — 90 seconds.** In **Material Workspace → PHREEQC planning**, show the exact runtime/database version, resource IDs, and SHA-256 provenance. Prepare one bounded synthetic leaching scenario. Read out the source-term mode and major assumptions. Show the generated `.pqi` text and the compatibility/limitations panel. Pause before execution and explain that preview is not execution and simulation is not validation.

3. **Explicit review, confirmation, and real software run — 60 seconds.** Review the exact input, then use the separate exact confirmation control. Execute only when readiness is green. Say: “This is a PHREEQC simulation under the displayed assumptions—not a measurement and not experimental validation.” If the runtime is unavailable or any gate is unclear, do not run; use the fallback and say execution was not performed.

4. **Results and immutable provenance — 45 seconds.** Open **Results** and then **Run History**. Show status, timestamp, input hash, executable hash, database hash, environment identity, app version, and resource-catalog/knowledge-pack provenance. Reopening history must retain the original resource identity; it must not rebind to a newer runtime/database.

5. **ICP QC and validation boundary — 60 seconds.** Open the **ICP Processor** machine and its Phase 3 prepare workflow. Use only the visibly prefilled synthetic test rows. Show supplied values, units, dilution factors, measured/predicted roles, corrections, QC codes, and the usable/censored/review-needed/excluded counts. Explain that only QC-eligible measured rows with a reviewed mapping and a predeclared criterion can support validation. Do not call these synthetic rows measurements of the demo material.

6. **Tentative XRD and reviewed evidence — 45 seconds.** Open **XRD advisory** and show its scientific limits: candidate overlaps are approximate/tentative and never confirmed phase identification. Open **Evidence** and show citation/source-location/review state. Literature is context, not a measurement of this material.

7. **Experiment design and sustainability — 45 seconds.** Open the **Experimental design** workspace and show an advisory plan with factor bounds, planned run count, and blank measurement fields. Then open **Sustainability screening** and show its declared boundary, supplied factors, excluded/missing factors, and screening-only status. Do not infer missing factors or claim an LCA result.

8. **AI configuration in three views — 60 seconds.** In **Settings & Diagnostics → Application settings**, first show **AI disabled** and the deterministic fallback. Then show the **Ollama local** configuration view: loopback/container gateway policy, explicit model discovery/pull confirmation, and no automatic download. Finally show the **cloud/administrator-hosted** configuration view and its privacy/credential-presence warning. Do not enter or display a credential. Explain that hosted endpoints/models are administrator-managed and AI cannot execute PHREEQC, promote resources, alter QC, or invent scientific facts.

9. **Close — 30 seconds.** Show **Settings & Diagnostics** with the USGS attribution/rights location and exact active resource identity. Summarize: PHREEQC is simulation; ICP validation needs measured QC-eligible evidence; XRD remains tentative; sustainability is screening; ML output requires a trained approved model and model card; AI is optional/advisory.

## Stop conditions

Stop the live route and use `NSF_DEMO_FALLBACK.md` if health, authentication, synthetic labels, exact resource identity, preview/confirmation, or provenance is missing or ambiguous. Never improvise a value, relabel synthetic output as measured, expose a secret, or report unavailable execution as successful.
