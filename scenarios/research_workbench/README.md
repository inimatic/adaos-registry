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

## What this milestone does not do

It does not run Codex, execute experiments, publish a paper, ingest directories
or archives, or use Ray. Those stages consume the accepted handoff later.
