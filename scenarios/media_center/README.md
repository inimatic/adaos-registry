# Media Center

Media Center is a Project-composed household media application. Its scenario is UI-as-data only; domain identity, indexing, playback, control, and byte delivery remain in skills, app shell, and core boundaries.

## Surfaces

The Project entrypoints declare explicit `desktop`, `tv`, `mobile_control`, and `embedded` presentation profiles independently of viewport width. The client selects the profile from `surfaceProfile` (or the `presentation_profile` entrypoint query), applies stable density/overscan/input semantics, and keeps the same catalog and control contracts. TV uses content rails and D-pad focus; mobile control puts Now Playing, target selection, and transport first while retaining Browse/Search below them.

The main page follows familiar media-center navigation: Home, Movies, Series, Music, Audiobooks, Folders, Playlists, Favorites, and Recent. Search is explicit-submit. Every catalog read uses opaque server cursors with 30 records per page. The universal `ui.list` renderer switches between list, grid, and rail layouts, uses browser rendering virtualization, and provides deterministic spatial keyboard/D-pad focus.

Home consumes the subscription-backed `media_center.library_state` snapshot. Favorites, recent state, catalog revision, and partial-agent status therefore converge across browsers in the same webspace without synchronizing the full catalog. Folder navigation remains an alternative first-class workflow.

Selecting an item, collection, folder, or playlist opens the playback modal. `build_playback_queue` returns endpoint-independent variant/route plans; the modal exposes only ten entries, while the control plane can persist a full bounded queue of 500. Closing the modal leaves the app-shell media element alive in the mini-player. Stop remains explicit.

The Remote modal is a controller surface for another webspace or TV. It lists registered playback targets, observes `media_control.now_playing`, and sends revision-safe transport intents through `media_control_skill`. A phone does not enter the media byte path.

Settings contains library roots, scan/import operations, profile/access and home-layout policy, playback defaults, metadata/rendition operations, agent resource/watch status, QoE, sanitized diagnostics, repair recommendations, and distributed deployment administration. Operators create a reviewed all-matching placement plan, explicitly apply its digest, and separately drain or remove an activation through public deployment tools. Folder import and scan calls have a ten-minute client budget and return asynchronous agent jobs when the distributed agent is active. Images remain disabled by policy. Original media bytes stay at their source paths; `.adaos` stores references, catalog state, jobs, playback state, and explicitly generated derived renditions only.

## Ownership

- Core owns reference registration, range delivery, project deployment, distributed service discovery/invocation, leases, fencing, and bounded topology projections.
- `media_library_agent` owns node-local roots, scans, probes, source revisions, and ordered deltas.
- `media_center_skill` owns the global read model, search, works/variants/collections, playlists, personalization projections, enrichment jobs, and playback plans.
- `media_control_skill` owns targets, persistent sessions, queues, commands, endpoint reconciliation, settings, and QoE.
- The client app shell owns the single live media element, Media Session integration, mini/full/PiP presentation, local high-frequency state, and direct-to-routed playback fallback.

Skill-specific human-readable errors and translations stay in each skill. The scenario does not bundle skill error dictionaries.

## Validation

`tests/fixtures/library-profile.v1.json` is the versioned 20,000-source household profile. Run the steady-state budget gate from the registry root:

```powershell
$env:PYTHONPATH='..\adaos\src'
python scenarios/media_center/benchmarks/run_library_benchmark.py --enforce
```

The harness reports one-time FTS/trigram backfill separately from p50/p95/max catalog FTS, cursor-page and local-discovery latency, encoded page bytes, process RSS, sample counts and the exact enforced budgets. Correctness assertions reject empty FTS/fuzzy results, incomplete search indexes and invalid page sizes. The same run migrates and removes 20,000 legacy works and collections, verifies 20,000 contextual works/memberships, and enforces a bounded migration budget. It uses generated descriptors and never needs private media bytes.
