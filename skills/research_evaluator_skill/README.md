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
| `C2_staged` | the compiled four-facet research contract |
| `C3_typed_execution` | compilation, AutomationBrief, and conformance fixture |
| `C4_over_specified` | C3 plus a prescribed implementation scaffold |

Every task must contain all five arms, paired seeds, both `fixed_downstream` and
`fixed_total_system` budgets, a frozen rubric, and at least one hidden evaluator
input. The primary endpoint is `evidence_valid_completion`: all mandatory checks
pass, the protocol has not drifted, the selected budget is respected, and no
unresolved failure was reported.

## Boundary and workflow

1. Build a task manifest whose file inputs carry immutable SHA-256 digests.
2. Call `freeze_calibration`; the task becomes immutable under its `task_id`.
3. For every arm, seed, and budget view call `prepare_calibration_arm` and give
   only the returned packet to the Builder/Codex execution boundary.
4. A separate judge records deterministic, expert, or LLM-judge check evidence
   through `record_calibration_result`.
5. `summarize_calibration` reports completion rate, Wilson intervals, resource
   usage, and first-failure stages for one budget view.

The evaluator owns its database. Research-direction skills own primary source
artifacts. Candidate packets contain materialized audience views and selected
instruction files; evaluator oracles and legacy implementations remain outside
those roots.
