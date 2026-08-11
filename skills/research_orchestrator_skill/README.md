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
4. Inspect `get_direction` and resolve blocking questions.
5. Call `accept_prototype` with the exact prototype digest, current generation,
   and an idempotency key. The command creates a private local Builder
   checkpoint, immutable AutomationBrief, and a least-write Builder Development
   Session. It uploads neither the direction source nor its intake artifacts.
6. Inspect the brief and use `open_builder_session`. Codex is deliberately not
   started by acceptance.

`get_activity` exposes the same durable stages used by grouped chat progress
and future Research Workbench widgets. `next_steps` is suitable for text and
voice channels.

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
