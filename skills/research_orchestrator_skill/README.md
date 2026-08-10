# Research Orchestrator Skill

The orchestrator is the reusable pre-Codex control component of AdaOS
Research Fabric. A research direction is one Builder skill project. The
orchestrator does not generate a scenario for every direction and does not own
experimental data or scientific governance records.

## First milestone

1. Create a skill with the `research_direction` Builder template.
2. Initialize it with `initialize_direction`.
3. Add notebooks and prose with Builder `source-add`, the Builder upload UI, or
   `attach_source`. Each change creates an immutable SourceBundle revision.
4. Use `chat` to discuss the sources. Every meaningful response is validated
   and stored as a ResearchPrototype revision; the transcript is not truth.
5. Inspect `get_direction` and resolve blocking questions.
6. Call `accept_prototype` with the exact prototype digest, current generation,
   and an idempotency key. The command creates an ordinary Builder/Forge
   checkpoint and an immutable AutomationBrief.
7. Inspect the brief. Codex is deliberately not started by acceptance.

`get_activity` exposes the same durable stages used by grouped chat progress
and future Research Workbench widgets. `next_steps` is suitable for text and
voice channels.

## Ownership

- Builder owns project source and package checkpoints.
- This skill owns formulation sessions, prototype revisions, acceptance, and
  Automation Briefs in its private relational binding.
- The direction skill owns experimental code and primary data.
- `research_manager_skill` remains the deterministic governance truth after
  Automation.
