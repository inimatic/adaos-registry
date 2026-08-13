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
   Notebook source cells are extracted before bounding, outputs are omitted,
   and every supplied fragment has an `artifact://...#cell/lines` reference.
4. Inspect `get_direction` and resolve blocking questions.
5. Call `accept_prototype` with the exact prototype digest, current generation,
   and an idempotency key. The command creates a private local Builder
   checkpoint, immutable AutomationBrief, and a least-write Builder Development
   Session. It uploads neither the direction source nor its intake artifacts.
6. Inspect the brief and use `open_builder_session`. Codex is deliberately not
   started by acceptance.

`get_activity` exposes the same durable stages used by grouped chat progress
and future Research Workbench widgets. Every formulation invocation first
records an `adaos.research.directive.v1` event with caller identity, origin,
visible directive text, and digest. A directive arriving through API, CLI, or
Codex is also projected into the research chat; a normal conversation message
is not duplicated because it is already in the transcript. Hidden system
instructions and source excerpts are never copied into this directive record.
Callers outside chat should pass stable `actor` and `invocation_origin` values.
`next_steps` is suitable for text and voice channels.

## Hard formulation boundary

The Root LLM proposes only the candidate-owned fields. AdaOS materializes and
digests `context_coverage` and `admission_review`; neither can be self-asserted
by the model. The deterministic review rejects or downgrades a handoff unless
it has:

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
- uniquely identified implementation requirements covering execution, data,
  reproducibility, observability and evidence, plus acceptance evidence that
  covers workflow, data integrity, reproducibility and evidence.

Generation and bounded repair request the Root provider's native JSON-object
output mode. Contract normalization may correct transport-only shape and enum
spelling, but never invents domain content. A structurally invalid candidate is
rejected after the bounded repair budget and no revision is stored. If a
schema-valid candidate still fails the hard gate after repair, it remains a
visible draft; completion messages state that explicitly and never call the
draft "ready". Neither case can silently become an AutomationBrief.

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
