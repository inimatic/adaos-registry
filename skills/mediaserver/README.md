# Mediaserver Target Architecture

Status: target contract and implementation checklist for the mediaserver stress
case. Phase 3 migration is implemented: the skill now publishes a summary-only
Yjs projection plus a bounded page route for media rows. Phase 4 is now focused
on post-write Yjs update amplification and runtime relaxation.

This skill is intentionally kept as a stress case while the core protections are
implemented. A skill-generated media library must not be able to overload the
shared Yjs document, the parent runtime process, or browser sessions. The
runtime should degrade, attribute, and explain unsafe payloads before they can
grow into YRoom pressure or sustained RSS growth.

## Incident Summary

The current mediaserver projection publishes a full media library into Yjs:

```text
skill:mediaserver -> ctx_subnet.set("mediaserver.library")
data/media/library -> data/nodes/<node_id>/media/library
```

With 1,520 files the original branch was already hundreds of KB. The current
skill no longer publishes row data into Yjs: `get_snapshot` writes a compact
summary and `mediaserver.list_library_page` returns bounded rows. However, the
2026-06-26 stand rollout showed that a 1.5 KB logical summary can still emit an
88 KB encoded Yjs update on the old branch. This points to retained Yjs branch
history/shared-doc encoding, not just current payload size.

The same post-write guard also identified `skill:infrastate_skill` with a
sub-1 KB payload producing a 465 KB encoded update. Mediaserver remains the
primary stress case, but the target architecture must protect the core against
any skill that triggers repeated Yjs update amplification.

## Target Data Contract

Yjs is only the reconnect-stable bootstrap plane:

- availability and readiness state
- total file count and aggregate bytes
- latest scan/index status
- compact capability summary
- freshness and degraded/quarantine markers
- links to details, search, page, and diagnostics routes

Yjs must not contain the full library, raw file paths, scan logs, playback
history, upload diagnostics, or stream/session internals.

Detailed media data belongs behind bounded routes:

- page/search tool or HTTP endpoint for library rows
- stream receiver for active scan/progress state
- explicit details tool for diagnostics
- disk/360log evidence for large failure artifacts

## Scale Assumption

The design target is a large household media library, not the current test
directory. Until real installation telemetry is available, treat 25,000-100,000
media rows as the normal large-family planning range and keep 500,000 rows as a
stress envelope, not the common case. The normal Yjs projection must stay
effectively constant size across that range.

The library route should support:

- pagination with stable cursor or offset
- search/filter by name, mime, modified time, and source
- small page size defaults
- cheap count/summary refresh
- bounded per-request response size
- no full-list materialization in the browser steady state

The first implemented route is `mediaserver.list_library_page`. It returns at
most 100 rows, supports cursor pagination, and keeps the browser off the
`data/media/library` Yjs path for row data. Offset remains accepted for small
diagnostic jumps, but cursor is the intended path for large libraries.

## Core Protection Contract

The runtime owns safety even when a skill is wrong:

- validate declared route budgets before publication
- reject or degrade oversized Yjs projections before mutating the primary doc
- preserve owner attribution for both current writer and amplified branch owner
- record payload bytes, item counts, path, slot, owner, and reason
- record encoded Yjs update bytes and amplification ratio after mutation
- surface guard state in reliability/status cards and skill-local repair
  evidence
- schedule compaction, path migration, quarantine, or room reset when repeated
  post-write amplification continues after payloads are compact
- allow the parent process memory to relax after pressure stops or after a
  guarded room reset/compaction

Unsafe skill behavior must result in a visible degraded/quarantined state and a
repair packet for the LLM Builder. It must not silently become long-term runtime
growth.

## Implementation Roadmap

1. Core guardrails first.
   Add machine-readable Yjs projection budgets, preflight size checks, and
   bounded degradation before any mediaserver behavior is fixed.

2. Core observability.
   Record projection payload size, item counts, owner, route, path, branch size,
   and write amplification suspects. Expose the summary through reliability and
   status cards.

3. Builder guidance.
   Update the LLM skill guide and templates so generated skills must choose a
   bounded data route before using Yjs.

4. Mediaserver migration.
   Change the projection to a constant-size summary. Move library rows to a
   paged/searchable details route and keep action responses as compact acks.
   The current implementation uses `data/media/library` only for count, bytes,
   freshness, capability, and route metadata.

5. Stress validation.
   Exercise the old and new behavior against synthetic large-library cases,
   confirm guard visibility, and confirm parent RSS plateaus or relaxes after
   pressure ends.

6. Yjs history recovery.
   When compact logical payloads still produce large encoded updates, migrate
   the projection to a fresh bounded branch or compact/reset the affected Yjs
   room with explicit repair evidence. This phase is complete only when
   repeated mediaserver refreshes stop producing large updates and RSS relaxes
   after warmup.

## Exit Criteria

- A full-list mediaserver projection is blocked or degraded by core guards.
- Reliability identifies `skill:mediaserver`, `mediaserver.library`, the Yjs
  path, payload bytes, encoded update bytes, and amplification ratio.
- A sibling node update no longer repeatedly emits hundreds of KB due to retained
  media or infrastate Yjs history.
- Mediaserver Yjs payload size remains within budget for 100k+ library rows.
- The full media library is available only through bounded page/search/detail
  routes.
- After a pressure burst, parent runtime RSS stops growing and relaxes after
  room cleanup/allocator trim instead of ratcheting indefinitely.
