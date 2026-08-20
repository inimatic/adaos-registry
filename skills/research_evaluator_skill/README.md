# Research Evaluator Skill

`research_evaluator_skill` is the independent measurement boundary for AdaOS
research-compilation experiments. It freezes a C0-C4 protocol, creates
audience-filtered input packets, keeps evaluator-only material out of candidate
contexts, and scores results from evidence references rather than self-reports.

## Controlled arms

| Arm | Candidate receives in addition to raw admitted artifacts |
| --- | --- |
| `C0_raw` | nothing |
| `C1_reviewed_prose` | one reviewed prose brief |
| `C2_staged` | the staged ResearchCompilation |
| `C3_typed_execution` | compilation, AutomationBrief, and conformance fixture |
| `C4_over_specified` | C3 plus a prescribed implementation scaffold |

Every task must contain all five arms, paired seeds, both `fixed_downstream` and
`fixed_total_system` budgets, a frozen rubric, and at least one hidden evaluator
input. The primary endpoint is `evidence_valid_completion`: all mandatory checks
pass, the protocol has not drifted, and no unresolved implementation or evidence
failure was reported. Resource compliance is a distinct secondary endpoint,
`budgeted_evidence_valid_completion`; token or wall-clock excess must not silently
turn a correct implementation into an incorrect one.

Calibration-task schema v1.7 makes that separation part of the frozen contract:
only a platform outage before candidate execution is excludable; post-start model,
schema, protocol, implementation, and evidence failures count against correctness,
while resource-budget excess affects only the budgeted secondary endpoint.
Visible execution contracts and hidden judge inputs are snapshotted into separate
owner-scoped content-addressed stores before freezing. A later source-tree edit can
therefore neither invalidate nor silently mutate an already frozen task.

For the primary AdaOS-versus-raw claim, calibration task v1.4 preregisters
`C0_raw` as control and `C3_typed_execution` as treatment. It requires at least
five pairs, a counterbalanced within-pair execution order, an
`incomplete_no_claim` missing-data policy, and a one-sided exact paired sign
test at alpha 0.05. With five pairs, five C3-only successes and no C0-only
successes yield `p=0.03125`; four discordant wins yield `p=0.0625` and do not
cross the threshold. The reported effect is the paired difference in
evidence-valid completion probability.

This is deliberately a local claim for the frozen TLP workload, AdaOS/core
commit, component versions, model profile, budgets, and environment. The
paired seed controls the scientific workload, not model sampling (which the
provider does not expose). Predeclared counterbalancing reduces execution-order
confounding but does not justify a universal autonomous-science claim.

## Boundary and workflow

1. Build a task manifest whose file inputs carry immutable SHA-256 digests.
2. Call `freeze_calibration`; the task becomes immutable under its `task_id`.
   To repeat an existing benchmark with a newly accepted formulation, call
   `derive_compact_calibration` with `source_direction_id` and optional
   `source_task_id`. The evaluator reads the exact accepted Compilation and
   AutomationBrief through the Orchestrator API, projects them to compact
   developer contracts, replaces the expected prototype digest, and points
   artifact materialization at that direction. It never reads the
   Orchestrator database or mutable source paths directly.
3. For every arm, seed, and budget view call `prepare_calibration_arm` and give
   only the returned packet to the Builder/Codex execution boundary.
4. A separate judge records deterministic, expert, or LLM-judge check evidence
   through `record_calibration_result`. `evaluate_builder_attempt` instead
   invokes the active ResearchManager consumer with
   `execute_workflow_smoke=true`; a terminal Builder status alone is never
   treated as evidence-valid completion.
5. `summarize_calibration` reports completion rate, Wilson intervals, resource
   usage, first-failure stages, and—when v1.3 or later is used—the independently
   recomputed paired C0/C3 result and scoped conclusion for one budget view.
6. `export_calibration_package` binds the frozen task, exact arm packets,
   immutable results, and recomputed summary into one content-addressed JSON
   object. Its digest is the portable audit identity; the database is not the
   only place from which the reported score can be reconstructed.

The evaluator owns its database. Research-direction skills own primary source
artifacts. Candidate packets contain materialized audience views and selected
instruction files; evaluator oracles and legacy implementations remain outside
those roots.

The C3 conformance fixture is the active public ResearchManager runner ABI,
including the JSON Schemas for `run_log.json`, `evaluation_audit.json`, and
`artifacts_index.json`. The judge may remain hidden, but it must not require an
undisclosed filename, shape, or seed-label convention. This separates genuine
implementation failure from benchmark-harness ambiguity.
