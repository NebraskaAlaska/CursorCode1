# NSF demo acceptance checklist

Record date, operator, commit, image digest, platform, exact commands, and actual results. Unchecked is not passed.

- [ ] Release scanner and dependency-lock validator pass.
- [ ] No raw/local data, databases, models, secrets, or workspaces enter any image target.
- [ ] Full tests pass inside the exact test image; actual count is recorded.
- [ ] Release runs as UID 10001 with a read-only root filesystem.
- [ ] PHREEQC version/archive/database hashes match the source manifest.
- [ ] Official example runs from `/opt/phreeqc/share/examples/` with `/opt/phreeqc/database/phreeqc.dat`.
- [ ] Local health and restart persistence pass.
- [ ] ICP template is header-only and comes from `release/templates/SYNTHETIC_DEMO_icp_template.csv`.
- [ ] AI is disabled or its consent/privacy/secret/spend path is reviewed.
- [ ] Demo project/scenario is visibly **SYNTHETIC DEMO**.
- [ ] Synthetic material composition is `user_assumption` + `unverified`, never measured.
- [ ] Input preview and explicit confirmation precede execution.
- [ ] Output is described as simulated, not measured/validated.
- [ ] Results and Run History retain input/runtime/database/environment/catalog provenance.
- [ ] ICP synthetic rows show units, dilution, roles, corrections, QC states, and eligibility.
- [ ] XRD is described as tentative/advisory; no candidate is called a confirmed phase.
- [ ] Evidence keeps citation/source/review state and is not presented as a measurement.
- [ ] Experimental design retains blank measurement fields and advisory status.
- [ ] Sustainability retains declared boundary, missing factors, and screening-only status.
- [ ] AI-disabled, local Ollama, and hosted/cloud configuration views are inspected without a secret.
- [ ] No secret, private record, participant identity, or confidential path appears.
- [ ] Backup/restore was tested on a disposable instance.
- [ ] Fallback artifacts match this commit/digest and retain labels.
- [ ] Release manifest records only produced evidence; promotion authorization is separate.
