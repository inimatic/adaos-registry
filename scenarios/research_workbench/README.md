# Research Workbench

Research Workbench is the single AdaOS application for managing research
directions before and across autonomous development. A direction is a durable
scientific lifecycle aggregate containing an agenda of bounded ResearchTasks;
it is not a distributable Project or skill. One isolated skill holds its source
artifacts. An implementation Project appears only when an accepted task creates
an ImplementationTrack. The Workbench does not create a separate scenario per
direction.

## Workflow

1. Create a direction with a stable Direction ID, human title, and description.
2. Select it in the portfolio.
3. Upload notebooks, Markdown, or other source material. Files are copied into
   the direction's artifact-custodian skill at `artifacts/part0/` and recorded by digest in
   `manifest.yaml`. Keep ordinary inputs `shared`; mark evaluator oracles such
   as a historical expert review `evaluation_only` before clean formulation.
4. Use Formulation chat to discuss the question, hypotheses, experiment stages,
   evaluation plan, uncertainty, constraints, and unresolved decisions. Chat is
   not canonical state; each candidate is a typed ResearchPrototype revision.
5. Open the full-width **Compilation** view. Inspect Source Analysis, Research
   Problem, Experimental Protocol, Engineering Contract, Experiment Plan, and the required
   source-to-acceptance traceability paths. Each facet and the aggregate
   package has an immutable digest.
6. Review the current exact prototype, source-context coverage, provenance,
   inference contract, and deterministic AdaOS admission review. The accept
   action is enabled only when both scientific admission and compilation pass.
7. Acceptance checkpoints the skill source and creates a digest-bound
   AutomationBrief plus a Builder Development Session. Codex is not started.
8. Open Builder. The full Project is the development envelope but only explicit
   Project-owned targets are read-write; the Workbench and
   orchestrator are contract context, while artifact inputs are immutable
   audience-scoped filesystem views. Hidden files are physically absent from
   Codex's artifact roots.
9. Choose **Start one-shot** to run Builder from those exact inputs. Use
   **Sync Builder** to import progress and failures into the durable Workbench
   journal; synchronization does not steer the agent.
10. After completion, prepare an isolated Project trial, inspect its candidate
    digest, and promote exactly that digest as a ProjectRelease.
11. Instantiate a Study. The installed direction skill must return immutable,
    distinct validation/robustness/test bindings with a sealed test split.
    Workbench then binds ResearchCompilation, ProjectRelease, runner,
    StudyRealization, and ResearchManager Experiment.
12. Start the bounded CPU smoke separately. It is workflow evidence only; use
    **Sync Study** to join attempt state and evidence into the same journal.

## Example formulation discussion

The useful unit of discussion is a decision that changes the structured
ResearchPrototype, not a request for a generic summary. For the TLP notebook
and review, a productive sequence is:

1. **Scope the claim.** “Read both artifacts. Separate what the historical
   notebook actually observed from claims it cannot support. Propose one
   falsifiable primary question about learnable max-plus pooling versus
   MaxPool. Do not treat notebook outputs as evidence.”
2. **Challenge the comparison.** “What confounders prevent a paired comparison?
   Require shared initialization, named RNG streams, the same data order and
   augmentation, and a sealed test split. Put unresolved facts into blocking
   questions rather than guessing.”
3. **Separate engineering from science.** “Define a three-epoch, one-seed CPU
   workflow smoke with `inference_allowed=false`, followed by a separately
   locked multi-seed scientific series. State stop conditions for both.”
4. **Lock evaluation.** “Choose one primary estimand, specify secondary
   mechanism diagnostics, uncertainty, multiplicity treatment, and a valid
   negative-result policy. Explain what would falsify each hypothesis.”
5. **Form the Codex boundary.** “Translate the agreed design into concrete
   implementation requirements and observable acceptance checks. Codex may
   modify only the selected Project target; Workbench, orchestrator,
   non-target members, and source artifacts are read-only. Ray remains
   deferred.”
6. **Audit readiness.** “Show remaining assumptions and blocking questions.
   Mark `ready_for_automation` only if Codex can implement the CPU smoke without
   inventing a scientific or infrastructure decision.”

After every turn, review the consensus panel. It is a projection of the current
typed candidate; chat history itself is not the accepted task. Only **Accept
exact prototype** creates the digest-bound AutomationBrief and unopened Builder
Development Session.

The portfolio and selected direction are separate full-surface layout variants
selected by local page state. Reload starts from the portfolio, a partially
materialized state cannot expose an empty detail view, and the detail workspace
can grow independently without being constrained to a permanent side panel.
The selected direction title opens the searchable selector. The outline may
select any task or implementation track for read-only inspection; formulation
writes remain bound to one explicitly activated task.
Compilation is itself a full-width conditional layout because the five facets
and traceability graph are primary work surfaces rather than sidebar metadata.

## Current boundary

The Workbench now governs one bounded autonomous implementation through an
installed local CPU Study. It does not yet authorize confirmatory inference,
autonomous protocol amendment, paper drafting, directory/archive ingestion,
or Ray execution. A three-epoch smoke validates workflow and instrumentation,
not the scientific hypothesis.
