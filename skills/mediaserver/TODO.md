# Mediaserver Roadmap Checklist

Status: active post-write amplification stress case. Keep the legacy full-list
projection evidence visible until core guards can explain and recover it.

## Phase 0 - Architecture Record

- [x] Capture incident shape and target architecture in `README.md`.
- [x] Treat mediaserver as a stress case for core guardrails before skill
  behavior is changed.

## Phase 1 - Core Protection Before Skill Fixes

- [x] Load Yjs projection budgets from skill `data_routes`.
- [x] Reject or degrade oversized Yjs projection payloads before primary-doc
  mutation.
- [x] Record attempted bytes, compacted bytes, item counts, path, slot, owner,
  and guard reason.
- [x] Preserve attribution when a large branch owned by one skill amplifies
  another writer's small node-scoped update.
- [x] Expose guard state in reliability and status cards.
- [x] Verify the current mediaserver projection is visible as an oversized
  projection anomaly.
- [x] Deploy guardrails to stand `.30` and capture before/after evidence.
- [x] Persist or forward projection guard telemetry from short-lived CLI/tool
  processes so runtime reliability sees operator-triggered guard decisions.
- [x] Add lifecycle observability for runtime migrations that stall on disk I/O,
  dependency installation, or SQLite locks.
- [x] Record post-write encoded Yjs update bytes and amplification ratio in
  runtime reliability so compact logical payloads can still be diagnosed.
- [x] Add active-slot marker reconciliation so an interrupted core update cannot
  leave `active=B` pointing at an empty slot and force mixed root/slot imports.

## Current Evidence - 2026-06-26

- Core guard simulation on stand `.30` with the real mediaserver snapshot:
  `count=1520`, raw result `419242` bytes, guarded projection
  `payload_bytes=402482`, `degraded_bytes=26969`, owner `skill:mediaserver`,
  slot `mediaserver.library`.
- Direct `adaos skill run` initially missed projection rules because
  tool-run processes did not load runtime `data_projections`; core now loads
  them from `resolved.manifest.json` before tool execution.
- Runtime API reliability currently reports only guard events from its own
  process in deployed `0.1.422+1.391f8d7`; local core now persists guard
  events to `state/observability/yjs_projection_guard.ndjson` so runtime
  reliability can aggregate events left by separate CLI/tool processes after
  the next deployment.
- Local core now uses those guarded projection rows as large-branch evidence:
  sibling writes in the same Yjs root/top-key domain (for example `data/nodes`)
  carry `last_write_amplification_suspects` in primary-doc governance and
  reliability, preserving the original branch owner (`skill:mediaserver`) even
  when another writer triggers the amplified update.
- Stand `.30` has severe I/O pressure: `/mnt/disk1` is full and PSI I/O was
  high during migration/testing. Heavy skill dependency installation can enter
  `D` state and cause SQLite lock symptoms.
- `mediaserver` runtime `0.8.4` is quarantined by migration because its pytest
  suite cannot find the webui schema from the runtime test CWD. Keep this as a
  skill repair item for the later mediaserver phase, not a core bypass.
- Local core now enriches `state/skill_runtime_migration/status.json` with
  `diagnostics`: stale age, current skill/stage, suspected blocker
  (`dependency_install_or_runtime_prepare_stalled`, `sqlite_lock`,
  `host_io_or_disk_pressure`, etc.), host disk/PSI hints, and recommended
  operator checks. The same payload is included in runtime reliability after
  deployment.
- After deployment to stand `.30` on active slot `B` (`0.1.429+1.5a0ac89`),
  `mediaserver.get_snapshot` returned a constant-size summary:
  `count=1534`, `items=[]`, logical `payload_bytes=1567`.
- The new post-write guard still recorded
  `reason=yjs_projection_write_amplification` for `skill:mediaserver`:
  `update_bytes=88494`, `amplification_ratio=56.474`,
  path `data/nodes/<node_id>/media/library`. This confirms the remaining
  problem is retained/shared Yjs update history, not current mediaserver row
  payload size.
- The same guard surfaced `skill:infrastate_skill`:
  `payload_bytes=984`, `update_bytes=465757`, `amplification_ratio=473.33`.
  Keep mediaserver as the stress case, but solve the core class for every
  skill-owned projection branch.
- Core rollout itself exposed a separate reliability defect: the old supervisor
  could stop a passive candidate and leave `active=B` without launching an
  active runtime until systemd restart. This is now documented as a core
  rollout-hardening task, separate from mediaserver behavior.
- The mediaserver summary projection has now moved to a fresh branch:
  slot `mediaserver.library_summary`, path `data/media/library_summary`. The
  legacy `data/media/library` branch remains only as incident evidence and is
  no longer a writer target.
- Local core now distinguishes live-room and detached writer recovery for
  post-write amplification. Detached skill-tool writes record
  `recovery.mode=inline_after_detached_write` and await YStore compaction before
  process exit; live-room writes use background compaction plus GC/allocator
  trim. Runtime reliability now includes a `yjs_projection_guard.recovery`
  section with inline/background counts and selected-webspace YStore replay
  tail context. Stand validation is still pending.
- Stand `.30` rollout of core `0.1.440+1.9227e72` exposed public runtime memory
  attribution: after boot the runtime family was about 1.0 GB, split between
  parent autostart runner and child skill processes. Top child RSS contributors
  were `neural_nlu_service_skill` (~249 MB), `rasa_nlu_service_skill` (~148 MB),
  `slideshow_skill` (~45 MB), `media_indexer_skill` (~43 MB), and
  `neuro_nlu_lite_skill` (~26 MB).
- The same rollout validated baseline maturation: cold baseline stayed in
  `warming`, then `maturity_blocked_slope`, and finally `mature` once slope
  settled. After five `mediaserver.get_snapshot` stress calls, family RSS
  later relaxed from about 1.07 GB to about 676 MB with `suspicion_state=stable`.
  This is a positive relaxation signal, but parent RSS still needs longer soak
  validation.

## Phase 2 - Builder Guidance

- [x] Split `docs/guides/llm-skill-development.md` guidance into smaller
  machine-checkable route contracts.
- [x] Require Yjs projection budgets for generated browser-facing skills.
- [x] Add examples for compact Yjs summary plus paged/search/details routes.
- [x] Add Builder repair evidence expectations for guard/quarantine events.

## Phase 3 - Mediaserver Migration

- [x] Change legacy `mediaserver.library` Yjs projection to a constant-size
  summary.
- [x] Move full library rows behind a bounded page route
  (`mediaserver.list_library_page`).
- [x] Keep `refresh_snapshot` as a compact acknowledgement, not a data
  transport.
- [x] Add count, total bytes, freshness, capability, and degraded-state fields
  to the Yjs summary.
- [x] Add pagination/search limits suitable for 100k+ rows, with stress margin
  toward 500k rows.
- [x] Add tests proving Yjs payload size does not scale with library row count.
- [ ] Add explicit search UI controls over `mediaserver.list_library_page`.
- [ ] Re-check media upload/delete refresh behavior in browser after switching
  the widget to skill-backed page data.

## Phase 4 - Stress And Relaxation Validation

- [ ] Run old mediaserver full-list projection as a blocked/degraded stress
  case.
- [ ] Run synthetic large-library tests at 10k, 100k, and 500k metadata rows.
- [ ] Confirm sibling node updates do not emit large media-driven Yjs diffs.
- [x] Confirm reliability shows owner, slot, path, payload bytes, encoded update
  bytes, amplification ratio, and suggested repair route.
- [ ] Implement or validate Yjs history recovery: projection path migration,
  room/doc compaction, or guarded room reset for branches that keep amplifying
  after logical payloads are compact.
- [x] Migrate mediaserver summary writes from the amplified legacy
  `data/media/library` branch to fresh `data/media/library_summary`.
- [x] Implement core post-write recovery modes for projection amplification:
  inline compaction for detached tool-run writers, background compaction for
  live-room writers, and allocator trim after compaction.
- [x] Surface projection recovery mode and YStore replay-tail context in
  reliability/CLI output.
- [ ] Deploy the post-write recovery core batch to stand `.30` and confirm
  repeated `mediaserver.get_snapshot` calls no longer leave a growing replay
  tail or sustained runtime RSS ratchet.
- [ ] Add an LLM Builder repair packet for repeated post-write amplification
  that names the owner, route, path, payload bytes, update bytes, ratio, and
  recommended bounded-route/compaction action.
- [ ] Harden rollout automation so root promotion, active slot launch, and
  active runtime port converge without manual systemd intervention.
- [ ] Confirm parent runtime RSS plateaus after warmup and relaxes after guard
  cleanup or room reset.

## Phase 5 - Runtime Memory Attribution And Relaxation

- [x] Capture runtime self-heal evidence before supervisor restarts an
  API-unready or listener-lost runtime.
- [x] Surface runtime self-heal decision/evidence in public update status,
  reliability, and CLI output.
- [x] Add runtime event-loop lag telemetry so API starvation is visible as a
  separate signal from RSS growth.
- [x] Verify on stand `.30` that the current long boot is an event-loop stall
  signal, not only a memory leak signal.
- [x] Expose current process RSS, child RSS, family RSS, sample freshness, and
  top child skill processes in supervisor memory status.
- [x] Deploy public memory attribution to stand `.30` and confirm it names the
  largest child runtimes during the mediaserver stress run.
- [x] Mature the memory baseline after warmup/import pressure so cold-start RSS
  does not become a permanent false growth reference.
- [ ] Add per-skill child-process memory policy hooks: observe first, then
  degrade/quarantine/restart only with Builder-facing evidence.
- [ ] Add long-window RSS relaxation validation: after mediaserver/Yjs pressure
  stops, family RSS must plateau and eventually drop or produce a named blocker.
- [ ] Feed memory attribution and event-loop/self-heal evidence into the LLM
  Builder repair packet format before updating skill guidance.
- [ ] After core protection is stable, update LLM skill guidance and then use
  mediaserver changes only to validate the guidance.

## Exit Criteria

- [ ] A skill cannot publish an unbounded Yjs list without a visible guard
  decision.
- [ ] Full media library access is page/search/detail driven.
- [ ] Yjs mediaserver state is reconnect-stable and constant-size.
- [ ] Memory growth from this incident class is bounded and recoverable.
