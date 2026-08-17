# Research Calibration Runner

This skill is the execution-side bridge for a frozen C0-C4 research-compiler
calibration. It does not own the task, rubric, or scores. Those remain inside
`research_evaluator_skill`.

`prepare_attempt` obtains one public packet through governed skill invocation,
creates a disposable one-skill research Project, materializes the packet's
audience-scoped source views, copies its exact typed instructions into a Builder
Development Session, and binds a private Builder host identity.

`start_attempt` then invokes normal `builder_sdk_control_skill.start_automation`.
Builder captures every read-only input into the isolated source snapshot under
`.adaos_context/<session>/`, outside the mutable candidate envelope. Codex sees
the resulting digest-bound access receipt but never evaluator-hidden material.

`get_attempt` reads the normal durable Builder Automation projection. Candidate
validation and scoring are intentionally performed later by the independent
evaluator.
