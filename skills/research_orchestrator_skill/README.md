# Research Orchestrator Skill

The orchestrator is the reusable pre-Codex control component of AdaOS
Research Fabric. A ResearchDirection is a scientific lifecycle aggregate in
the Workbench index, not a Project, skill, Builder session, or UI node. A
separate skill is its physical artifact custodian. Implementation Projects are
created only for accepted ResearchTask/ImplementationTrack handoffs. The
orchestrator does not generate a scenario for every direction and does not own
experimental data or post-handoff scientific governance records.

## First milestone

1. Open the shared `research_workbench` application and create a direction.
   `create_direction` atomically creates the domain record and a project-only
   artifact-custodian skill through the Builder SDK. It does not create an
   implementation Project yet.
2. Add notebooks and prose in Workbench. `attach_source` copies them into the
   target skill's `artifacts/part0/` and updates its digest-bound manifest.
   Choose an explicit stage visibility profile when a source is not shared.
   `evaluation_only` is the correct profile for a hidden expert oracle such as
   the historical TLP initial review.
3. Use `chat` to discuss the sources. Every meaningful response is validated
   and stored as a ResearchPrototype revision; the transcript is not truth.
   Notebook code and Markdown are parsed before bounding; imports, definitions,
   literal configuration, query-relevant cells, near-duplicate revisions, and
   bounded historical-output summaries are represented separately. Historical
   outputs remain explicitly exploratory/untrusted. Every supplied fragment has
   an exact `artifact://...#cell/lines` reference.
4. Inspect `get_compilation`: Source Analysis, Research Problem, Experimental
   Protocol, Engineering Contract, and the deterministic Experiment Plan have independent digests and a
   source-to-acceptance traceability report. Resolve blocking questions.
5. Call `accept_prototype` with the exact prototype digest, current generation,
   and an idempotency key. The command creates an ImplementationTrack, its
   implementation Project, a private local Builder checkpoint, immutable
   task-bound ResearchCompilation and AutomationBrief, and a least-write
   Project-scoped Builder Development Session. It uploads neither the direction
   source nor its intake artifacts.
6. Inspect the brief and use `open_builder_session`. Codex is deliberately not
   started by acceptance. `start_implementation` is the explicit one-shot
   transition; it can only use the exact Development Session instruction.
7. Use `sync_implementation`, then explicitly prepare and promote the normal
   Builder candidate with `prepare_project_release` and
   `publish_project_release`. The track stores both the candidate digest and
   the exact promoted `ProjectRelease` identity.
8. `instantiate_study` asks the installed runner for immutable validation,
   robustness, and sealed-test split bindings, creates a manager-owned
   `StudyRealization` and Experiment, and binds both back to the track.
   `start_study_smoke` is the separate confirmed transition that locks the
   compiled protocol and submits the non-inferential CPU preflight.

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

The default path uses three bounded LLM stages: `problem_frame`,
`protocol_design`, and `implementation_contract`. AdaOS then compiles five
stable facets: `source_analysis`, `research_problem`,
`experimental_protocol`, `engineering_contract`, and the provider-neutral
`experiment_plan`. The protocol stage must assign
stable arm ids and one exact primary minuend/subtrahend. Since formulation
stage contract v1.2, `problem_frame.experimental_signature` freezes only the
cross-stage scientific identity: subject, dataset, baseline, intervention,
intervention boundary, and primary outcome. Protocol admission requires exact
machine identities and exact outcome/boundary copies from that signature.
The legacy comparator array may use either the complete ordered ids or the
complete ordered labels; mixed or foreign identities fail closed;
`implementation_contract.scientific_bindings` then binds the exact protocol
digest, those identities, and `adaos.research.runner.v1`. This narrow typed
spine prevents a locally schema-valid protocol for a different experiment
from being assembled with the original question while leaving rationale,
method detail, and implementation strategy open to the model. Repairs always
receive the original directive and upstream typed artifacts as immutable
authority; a rejected candidate is never their only context. The deterministic
plan then carries exact execution profiles and integer RNG seeds. Pairing
allocation units must be the same ordered integer values; symbolic labels such
as `S1` cannot masquerade as executable seeds. It also carries evidence
classes, data policy, RNG streams, estimand and the public
runner/split-binding ABI without selecting a runtime provider. Runner results
use the domain-neutral canonical fields `primary_metric`, `step`,
`pairing_identity_digest`, `arm_id`, `seed`, and `evidence_class`; scientific
metric names remain plan data rather than framework code. It also creates a
digest-bound traceability graph and fails the compilation gate when required
source-to-acceptance paths are absent. The Root LLM proposes only each bounded
stage artifact. AdaOS materializes and
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
  classes, including exactly one AdaOS-policy CPU smoke with three epochs and
  seed 17;
- non-empty unique integer `budget.seed_values`, with the confirmatory values
  exactly mirrored by `pairing.allocation.planned_units`;
- exact cross-stage preservation of experimental identity and an engineering
  binding to the accepted protocol digest;
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

Artifact visibility is an enforced filesystem boundary. Each item has a
generic core `context_policy`; this skill maps research profiles to the exact
`research.formulation`, `research.implementation`, and
`research.evaluation` audiences. Acceptance materializes immutable filtered
views and binds their digests into Builder's Development Session. Hidden files
are not present under Codex's read-only artifact roots.

## Ownership

- This skill owns the local ResearchDirection index, ResearchAgenda/Task,
  formulation sessions, accepted ResearchCompilation, ImplementationTrack,
  prototype revisions, acceptance, aliases, and AutomationBriefs in its private
  relational binding.
- The artifact-custodian skill owns intake bytes and manifests. Logical
  ownership is explicit in the direction/task projection; other skills receive
  path-free, audience-filtered SDK projections rather than its raw DB binding.
- A Project owns implementation/distribution composition; Builder owns component
  source and package checkpoints. Private pre-Codex checkpoints stay in local
  CTX state; ordinary Forge publication starts only after implementation and
  review.
- Selecting a task is a read operation. `select_active_task` is the explicit
  authority transition required before formulation may write that task.
- `research_manager_skill` remains the deterministic governance truth after
  Automation.

Builder, manager, and later evaluator status snapshots are federated into the
orchestrator journal with `(origin, source_event_id)` replay identity. The
Workbench therefore observes one durable lifecycle without copying another
component's private database or making UI connectivity part of execution.

The Codex-facing AutomationBrief v1.6 is an executable projection rather than
a second prose specification. It preserves every implementation requirement
and acceptance identity plus the exact runner/provider boundary, while
removing verification prose already carried by the scientific compilation and
the consumer-owned conformance fixture. Its predecessor digest keeps the full
accepted brief auditable.
