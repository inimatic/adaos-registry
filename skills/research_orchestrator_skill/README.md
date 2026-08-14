# Research Orchestrator Skill

The orchestrator is the reusable pre-Codex control component of AdaOS
Research Fabric. A research direction is one Builder skill project. The
orchestrator does not generate a scenario for every direction and does not own
experimental data or scientific governance records.

## First milestone

1. Open the shared `research_workbench` application and create a direction.
   `create_direction` atomically creates an `adaos.project.v1` declaration and
   its primary `research_direction` skill through the Builder SDK.
2. Add notebooks and prose in Workbench. `attach_source` copies them into the
   target skill's `artifacts/part0/` and updates its digest-bound manifest.
3. Use `chat` to discuss the sources. Every meaningful response is validated
   and stored as a ResearchPrototype revision; the transcript is not truth.
   Notebook code and Markdown are parsed before bounding; imports, definitions,
   literal configuration, query-relevant cells, near-duplicate revisions, and
   bounded historical-output summaries are represented separately. Historical
   outputs remain explicitly exploratory/untrusted. Every supplied fragment has
   an exact `artifact://...#cell/lines` reference.
4. Inspect `get_direction` and resolve blocking questions.
5. Call `accept_prototype` with the exact prototype digest, current generation,
   and an idempotency key. The command creates a private local Builder
   checkpoint, immutable AutomationBrief, and a least-write Builder Development
   Session. It uploads neither the direction source nor its intake artifacts.
6. Inspect the brief and use `open_builder_session`. Codex is deliberately not
   started by acceptance.

`get_activity` exposes compact durable stages used by grouped chat progress and
future Research Workbench widgets. `get_formulation_run` exposes the exact stage
payloads, provider/model identity, all job attempts, aggregate token use,
schema/input/output digests, and repair count for forensic review. Every
formulation invocation first
records an `adaos.research.directive.v1` event with caller identity, origin,
visible directive text, and digest. A directive arriving through API, CLI, or
Codex is also projected into the research chat; a normal conversation message
is not duplicated because it is already in the transcript. Hidden system
instructions and source excerpts are never copied into this directive record.
Callers outside chat should pass stable `actor` and `invocation_origin` values.
`next_steps` is suitable for text and voice channels.

## Hard formulation boundary

The default path is a three-stage pipeline: `problem_frame`,
`protocol_design`, and `implementation_contract`. The Root LLM proposes only
each bounded stage artifact. AdaOS materializes and
digests `context_coverage` and `admission_review`; neither can be self-asserted
by the model. Provider-native Structured Outputs constrain shape; AdaOS retains
the richer local JSON Schema and semantic gates because provider schemas accept
only a subset of JSON Schema. One repair reruns only the failed stage.

Early discovery questions do not become final blockers automatically. Every
protocol decision is typed as source-derived, policy-default, proposed, or
unresolved. Only an unresolved decision may carry a blocking question. AdaOS
compiles source references, ids, readiness, user-facing completion text,
checkpoint-selection semantics, and the primary three-way decision rule rather
than asking the model to reproduce them consistently in prose.

The deterministic review rejects or downgrades a handoff unless it has:

- exact provenance references for independent observations and every
  hypothesis, with hypothesis claims forbidden from masquerading as observed
  source facts;
- separate workflow-smoke and confirmatory stages with correct evidence
  classes;
- explicit comparators, paired invariants/varied factors, a predeclared
  allocation of paired units, named RNG streams, data sealing, and leakage
  controls;
- one operationalized primary estimand, one primary outcome, uncertainty,
  multiplicity, practical significance, stopping, and a negative-result
  policy;
- a typed effect direction and practical threshold whose supported,
  contradicted/equivalent, and inconclusive interval regions are compiled by
  AdaOS;
- an explicit development/selection policy and sealed final-test policy that
  prohibits test feedback and per-epoch final-test observation;
- uniquely identified implementation requirements covering execution, data,
  reproducibility, observability and evidence, plus acceptance evidence that
  covers workflow, data integrity, reproducibility and evidence.

The research workload uses a Root-managed high-capability model profile
(`development` by default, overrideable with
`ADAOS_RESEARCH_LLM_PROFILE_SCOPE`) and does not embed provider credentials or a
model id. Contract normalization only projects the local schema onto the
provider-supported subset; it never invents domain content. A structurally or
semantically invalid stage is rejected after the bounded repair budget and no
revision is stored. A valid candidate with a genuinely unresolved decision
remains a visible draft. Neither case can silently become an AutomationBrief.

## Ownership

- The Project declaration owns distribution composition; Builder owns component
  source and package checkpoints. Private pre-Codex checkpoints stay in local
  CTX state; ordinary Forge publication starts only after implementation and
  review.
- This skill owns formulation sessions, prototype revisions, acceptance, and
  Automation Briefs in its private relational binding.
- The direction skill owns intake artifacts, experimental code, and primary data.
- `research_manager_skill` remains the deterministic governance truth after
  Automation.
