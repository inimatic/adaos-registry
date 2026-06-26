# Mediaserver Roadmap Checklist

Status: active stress case. Do not hide the current full-list projection until
core guards can detect and explain it.

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
- [ ] Preserve attribution when a large branch owned by one skill amplifies
  another writer's small node-scoped update.
- [x] Expose guard state in reliability and status cards.
- [x] Verify the current mediaserver projection is visible as an oversized
  projection anomaly.
- [x] Deploy guardrails to stand `.30` and capture before/after evidence.
- [ ] Persist or forward projection guard telemetry from short-lived CLI/tool
  processes so runtime reliability sees operator-triggered guard decisions.
- [ ] Add lifecycle observability for runtime migrations that stall on disk I/O,
  dependency installation, or SQLite locks.

## Current Evidence - 2026-06-26

- Core guard simulation on stand `.30` with the real mediaserver snapshot:
  `count=1520`, raw result `419242` bytes, guarded projection
  `payload_bytes=402482`, `degraded_bytes=26969`, owner `skill:mediaserver`,
  slot `mediaserver.library`.
- Direct `adaos skill run` initially missed projection rules because
  tool-run processes did not load runtime `data_projections`; core now loads
  them from `resolved.manifest.json` before tool execution.
- Runtime API reliability currently reports only guard events from its own
  process; separate CLI-process guard events are not yet aggregated.
- Stand `.30` has severe I/O pressure: `/mnt/disk1` is full and PSI I/O was
  high during migration/testing. Heavy skill dependency installation can enter
  `D` state and cause SQLite lock symptoms.
- `mediaserver` runtime `0.8.4` is quarantined by migration because its pytest
  suite cannot find the webui schema from the runtime test CWD. Keep this as a
  skill repair item for the later mediaserver phase, not a core bypass.

## Phase 2 - Builder Guidance

- [ ] Split `docs/guides/llm-skill-development.md` guidance into smaller
  machine-checkable route contracts.
- [ ] Require Yjs projection budgets for generated browser-facing skills.
- [ ] Add examples for compact Yjs summary plus paged/search/details routes.
- [ ] Add Builder repair evidence expectations for guard/quarantine events.

## Phase 3 - Mediaserver Migration

- [ ] Change `mediaserver.library` Yjs projection to a constant-size summary.
- [ ] Move full library rows behind a bounded page/search route.
- [ ] Keep `refresh_snapshot` as a compact acknowledgement, not a data
  transport.
- [ ] Add count, total bytes, freshness, capability, and degraded-state fields
  to the Yjs summary.
- [ ] Add pagination/search limits suitable for 100k+ rows, with stress margin
  toward 500k rows.
- [ ] Add tests proving Yjs payload size does not scale with library row count.

## Phase 4 - Stress And Relaxation Validation

- [ ] Run old mediaserver full-list projection as a blocked/degraded stress
  case.
- [ ] Run synthetic large-library tests at 10k, 100k, and 500k metadata rows.
- [ ] Confirm sibling node updates do not emit large media-driven Yjs diffs.
- [ ] Confirm reliability shows owner, slot, path, payload bytes, item count,
  and suggested repair route.
- [ ] Confirm parent runtime RSS plateaus after warmup and relaxes after guard
  cleanup or room reset.

## Exit Criteria

- [ ] A skill cannot publish an unbounded Yjs list without a visible guard
  decision.
- [ ] Full media library access is page/search/detail driven.
- [ ] Yjs mediaserver state is reconnect-stable and constant-size.
- [ ] Memory growth from this incident class is bounded and recoverable.
