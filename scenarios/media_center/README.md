# Media Center

Media Center is a Project-composed household media application. Its scenario is UI-as-data only; domain identity, indexing, playback, control, and byte delivery remain in skills, app shell, and core boundaries.

Version `0.6.12` moves metadata operation observability to the coordinator's
bounded subscription stream for Project `0.6.48`; the rest of the UI-as-data
behavior is unchanged from `0.6.9`.

## Surfaces

The Project entrypoints declare explicit `desktop`, `tv`, `mobile_control`, and `embedded` presentation profiles independently of viewport width. The client selects the profile from `surfaceProfile` (or the `presentation_profile` entrypoint query), applies stable density/overscan/input semantics, and keeps the same catalog and control contracts. TV uses content rails and D-pad focus; mobile control puts Now Playing, target selection, and transport first while retaining Browse/Search below them.

The main page follows familiar media-center navigation: Home, Movies, Series, Music, Audiobooks, Folders, Playlists, Favorites, and Recent. A single adaptive UI-as-data toolbar combines Remote, profile, section, layout, and Settings without media-specific client code. Its native buttons and menus work with pointer, touch, keyboard, and TV D-pad input; the mobile-control profile keeps its transport-first surface. Search is explicit-submit. Every catalog read uses opaque server cursors with 30 records per page. The universal `ui.list` renderer switches between list, grid, and carousel layouts, uses browser rendering virtualization, and provides deterministic spatial keyboard/D-pad focus.

Home consumes the subscription-backed `media_center.library_state` snapshot. It keeps the loading state until the first snapshot and maps the skill-owned collection state to distinct indexing, unconfigured, configured-empty, unavailable, and profile-empty presentations. Favorites, recent state, catalog revision, and partial-agent status therefore converge across browsers in the same webspace without synchronizing the full catalog. Folder navigation remains an alternative first-class workflow.

Selecting an item, collection, folder, or playlist opens the playback modal. `build_playback_queue` returns endpoint-independent variant/route plans; the modal exposes only ten entries, while the control plane can persist a full bounded queue of 500. Closing the modal leaves the app-shell media element alive in the mini-player. Stop remains explicit.

Media Center modals are workspace-scoped UI-as-data surfaces. The client therefore does not stamp the currently displayed node onto coordinator reads or actions; source-node selection remains an explicit playback-plan concern. This keeps the same modal valid on desktop, TV, controller, and federated-node views.

The Remote modal is a controller surface for another webspace or TV. Opening playback admits the current browser/TV as a durable endpoint session through the queue's generic control adapter. The app shell publishes meaningful state changes and bounded checkpoints, consumes session-scoped revisioned commands, and survives closing the player modal. Remote lists registered playback targets, subscribes to `media_control.now_playing`, and sends revision-safe transport intents through `media_control_skill`; a phone does not enter the media byte path.

Settings contains library roots, scan/import operations, profile/access and home-layout policy, playback defaults, metadata/rendition/artwork operations, agent resource/watch status, QoE, sanitized diagnostics, repair recommendations, and distributed deployment administration. Metadata operations subscribe to the coordinator's bounded `media_center.operation_state` stream; artwork progress subscribes to the agent's bounded durable rendition stream. Both remain observable without rebuilding home shelves or putting job history into synchronized page state. Autoplay and auto-fullscreen use the generic `input.toggle` UI-as-data contract and read their authoritative profile values from `media_control_skill`; they are not mirrored in page-local state. Operators create a reviewed all-matching placement plan, explicitly apply its digest, and separately drain or remove an activation through public deployment tools. Folder import and scan calls have a ten-minute client budget and return asynchronous agent jobs when the distributed agent is active. Images remain disabled as catalog media by policy; normalized artwork is a derived presentation resource. Original media bytes stay at their source paths; `.adaos` stores references, catalog state, jobs, playback state, and explicitly generated derived renditions only.

## Ownership

- Core owns reference registration, range delivery, project deployment, distributed service discovery/invocation, leases, fencing, and bounded topology projections.
- `media_library_agent` owns node-local roots, scans, probes, source revisions, and ordered deltas.
- `media_center_skill` owns the global read model, search, works/variants/collections, playlists, personalization projections, enrichment jobs, and playback plans.
- `media_control_skill` owns targets, persistent sessions, queues, commands, endpoint reconciliation, settings, and QoE.
- The client app shell owns the single live media element, Media Session integration, mini/full/PiP presentation, local high-frequency state, and direct-to-routed playback fallback.

Skill-specific human-readable errors and translations stay in each skill. The scenario references `runtime.media_center.ui.*` keys with English fallbacks but does not bundle dictionaries; `media_center_skill` owns both EN and RU resources.

## Validation

`tests/fixtures/library-profile.v1.json` is the versioned 20,000-source household profile. Run the steady-state budget gate from the registry root:

```powershell
$env:PYTHONPATH='..\adaos\src'
python scenarios/media_center/benchmarks/run_library_benchmark.py --enforce
```

The sustained catalog/playback acceptance gate uses a real agent-delta writer
beside bounded FTS, cursor-page and playback-plan readers. Acceptance mode
cannot be shortened below one hour or reduced below 20,000 catalog items:

```bash
python scenarios/media_center/benchmarks/run_media_center_soak.py \
  --acceptance --enforce
```

Use `--duration-seconds 60 --count 2000 --enforce` only as a development
smoke test; it is intentionally not accepted as long-duration evidence.

The static large-library gate keeps a 150 ms FTS p95 budget. The concurrent
one-hour gate allows 200 ms while continuous agent deltas hold the write path.
Memory evidence records absolute RSS, the complete steady-state range, and p95
for bounded initial/final windows. Only continued window-to-window growth is a
leak failure; a one-time allocator or SQLite cache high-water mark remains
visible but is governed by the separate 350 MiB absolute limit.

The 2026-08-21 local server acceptance run passed for 3,600.063 seconds with
20,000 catalog items and 307,950 applied agent deltas. It recorded no operation
errors; p95 was 64.079 ms for FTS, 57.656 ms for catalog pages, 33.881 ms for
playback plans, and 120.926 ms for delta application. RSS peaked at 39.02 MiB
with 0.793 MiB sustained growth, and aggregate CPU p95 was 13.533%. This is
server evidence only; the separate one-hour Android TV renderer gate remains
mandatory.

The harness reports one-time FTS/trigram backfill separately from p50/p95/max catalog FTS, cursor-page and local-discovery latency, encoded page bytes, process RSS, sample counts and the exact enforced budgets. Correctness assertions reject empty FTS/fuzzy results, incomplete search indexes and invalid page sizes. The same run migrates and removes 20,000 legacy works and collections, verifies 20,000 contextual works/memberships, and enforces a bounded migration budget. It uses generated descriptors and never needs private media bytes.
