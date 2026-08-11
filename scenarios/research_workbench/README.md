# Research Workbench

Research Workbench is the single AdaOS application for managing research
directions before autonomous development starts. A direction is a distributable
`adaos.project.v1` Project whose primary component is one
`adaos.research.direction.v1` skill. The Workbench does not create a separate
scenario per direction.

## Workflow

1. Create a direction with a stable Project ID, human title, and description.
2. Select it in the portfolio.
3. Upload notebooks, Markdown, or other source material. Files are copied into
   the direction skill at `artifacts/part0/` and recorded by digest in
   `manifest.yaml`.
4. Use Formulation chat to discuss the question, hypotheses, experiment stages,
   evaluation plan, uncertainty, constraints, and unresolved decisions. Chat is
   not canonical state; each candidate is a typed ResearchPrototype revision.
5. Review the current exact prototype and accept it only when its readiness is
   `ready_for_automation` and blocking questions are empty.
6. Acceptance checkpoints the skill source and creates a digest-bound
   AutomationBrief plus a Builder Development Session. Codex is not started.
7. Open Builder. Only Project-owned targets are read-write; the Workbench,
   orchestrator, and artifact groups are explicit read-only context.

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
   modify only the direction skill; Workbench, orchestrator, and source
   artifacts are read-only. Ray remains deferred.”
6. **Audit readiness.** “Show remaining assumptions and blocking questions.
   Mark `ready_for_automation` only if Codex can implement the CPU smoke without
   inventing a scientific or infrastructure decision.”

After every turn, review the consensus panel. It is a projection of the current
typed candidate; chat history itself is not the accepted task. Only **Accept
exact prototype** creates the digest-bound AutomationBrief and unopened Builder
Development Session.

## What this milestone does not do

It does not run Codex, execute experiments, publish a paper, ingest directories
or archives, or use Ray. Those stages consume the accepted handoff later.
