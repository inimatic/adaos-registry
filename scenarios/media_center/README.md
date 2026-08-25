# Media Center

Media Center is a Project-composed household media application. Its scenario is UI-as-data only; domain identity, indexing, playback, control, and byte delivery remain in skills, app shell, and core boundaries.

Version `0.6.13` adds a path-centric folder browser for Project `0.6.52`.
Folders and direct media share one cursor-backed page, but declarative typed
selection sends a folder into drill-down and sends only a media item to the
player. Breadcrumbs navigate back without materializing a directory tree.

## Surfaces

The adaptive UI-as-data toolbar includes a compact Filter and sort modal. It navigates server-provided genre and year facets, applies a minimum rating, and exposes title/date/rating/duration/progress/play-count/resolution/bitrate/random orders for list, grid, and carousel layouts.

The Project entrypoints declare explicit `desktop`, `tv`, `mobile_control`, and `embedded` presentation profiles independently of viewport width. The client selects the profile from `surfaceProfile` (or the `presentation_profile` entrypoint query), applies stable density/overscan/input semantics, and keeps the same catalog and control contracts. TV uses content rails and D-pad focus; mobile control puts Now Playing, target selection, and transport first while retaining Browse/Search below them.

The main page follows familiar media-center navigation: Home, Movies, Series, Music, Audiobooks, Folders, Playlists, Favorites, and Recent. A single adaptive UI-as-data toolbar combines Remote, profile, section, layout, and Settings without media-specific client code. Its native buttons and menus work with pointer, touch, keyboard, and TV D-pad input; the mobile-control profile keeps its transport-first surface. Search is explicit-submit and has an explicit Reset action that clears both the query field and authoritative catalog filter. Every catalog read uses opaque server cursors with 30 records per page. Selecting a series, album, or audiobook opens a bounded collection browser with breadcrumbs, child seasons/parts, representative artwork, and explicit Play All; selecting it never materializes an unbounded queue in the page. The universal `ui.list` renderer switches between list, grid, and carousel layouts, uses browser rendering virtualization, and provides deterministic spatial keyboard/D-pad focus. Carousel arrows move one bounded viewport while retaining focus on the arrow, so repeated TV-remote clicks remain stable.

Home consumes the subscription-backed `media_center.library_state` snapshot. It keeps the loading state until the first snapshot and maps the skill-owned collection state to distinct indexing, unconfigured, configured-empty, unavailable, and profile-empty presentations. Favorites, recent state, catalog revision, and partial-agent status therefore converge across browsers in the same webspace without synchronizing the full catalog. Folder navigation remains an alternative first-class workflow: Home uses typed UI-as-data selection, so a folder shelf item enters the path browser instead of creating a playback queue. The first folder page contains configured library roots, then drills down within the selected agent/root scope. Root and breadcrumb selections carry those identities in page state, so similarly named folders on different disks or nodes cannot cross-navigate and a root card never opens the player.

Selecting an item, collection, or playlist opens the playback modal. Selecting
a folder enters that folder; selecting a direct media file inside it opens the
player. `build_playback_queue` returns endpoint-independent variant/route plans;
the modal exposes only a ten-entry window around the selected item, while the
app-shell coordinator and control plane retain the full bounded queue of 500.
Opening an episode or track preserves its current collection as queue ownership
and starts at that exact item. The modal profile selector keeps personal,
household, and kids policy visible while playback is being initiated. Closing
the modal leaves the app-shell media element alive
in the mini-player. Stop remains explicit. The mini-player exposes separate
Stop and Close commands: Stop resets playback but leaves the source ready,
while Close persists the resumable position, unloads the source, and removes
the mini-player surface.

Media Center modals are workspace-scoped UI-as-data surfaces. The client therefore does not stamp the currently displayed node onto coordinator reads or actions; source-node selection remains an explicit playback-plan concern. This keeps the same modal valid on desktop, TV, controller, and federated-node views.

The Remote modal is a controller surface for another webspace or TV. Opening playback admits the current browser/TV as a durable endpoint session through the queue's generic control adapter. The app shell publishes meaningful state changes and bounded checkpoints, consumes session-scoped revisioned commands, and survives closing the player modal. Remote presents a compact target selector with device, endpoint, authorization and availability context, a human-titled Now Playing row for that target, and one transport toolbar. It subscribes to an immediately seeded, mutation-maintained `media_control.now_playing` projection and sends revision-safe transport intents through `media_control_skill`; a phone does not enter the media byte path. `media_control_skill` also contributes a standalone desktop icon and compact remote widget for control outside the Media Center scenario.

Settings contains library roots, scan/import operations, profile/access and home-layout policy, playback defaults, agent resource/watch status, QoE, sanitized diagnostics, repair recommendations, and distributed deployment administration. Its universal details surface subscribes to `media_library_agent.progress`, so a queued or running scan shows the current root and bounded counters and a failed scan shows the durable diagnostic; root rows retain their last scan timestamp and status. Metadata and artwork activity are secondary and open in a dedicated modal; only that modal subscribes to the bounded `media_center.operation_state` and durable rendition-progress streams. They remain observable without adding background subscriptions to the main Settings surface, rebuilding home shelves, or putting job history into synchronized page state. Autoplay and auto-fullscreen use the generic `input.toggle` UI-as-data contract and read their authoritative profile values from `media_control_skill`; they are not mirrored in synchronized page state. The queue descriptor carries the effective values to the app-shell player, and the client caches the last confirmed profile preference only to make the next user gesture available for native fullscreen. Video selection prearms the player shell while browser activation is still valid; the persistent media element is attached when its source arrives, so slow first loads and resumed playback follow the same path and the diagnostic overlay remains visible in fullscreen. A server preference of `false` cancels a stale prearm as soon as the queue contract arrives. A failed media-element load exits native/schema fullscreen, records a bounded endpoint-local compatibility verdict, and presents Retry, Convert when a derived browser version is applicable, and Cancel over the player. Convert queues an exact-source-bound background rendition through `media_center_skill.ensure_rendition`; it never overwrites or relocates the original. Operators create a reviewed all-matching placement plan, explicitly apply its digest, and separately drain or remove an activation through public deployment tools. Folder import and scan calls have a ten-minute client budget and return asynchronous agent jobs when the distributed agent is active. Images remain disabled as catalog media by policy; normalized artwork is a derived presentation resource resolved by the app shell through AdaOS media transport. Original media bytes stay at their source paths; `.adaos` stores references, catalog state, jobs, playback state, and explicitly generated derived renditions only.

## Ownership

- Core owns reference registration, range delivery, project deployment, distributed service discovery/invocation, leases, fencing, and bounded topology projections.
- `media_library_agent` owns node-local roots, scans, probes, source revisions, and ordered deltas.
- `media_center_skill` owns the global read model, search, works/variants/collections, playlists, personalization projections, enrichment jobs, and playback plans.
- `media_control_skill` owns targets, persistent sessions, queues, commands, endpoint reconciliation, settings, and QoE.
- The client app shell owns the single live media element, Media Session integration, mini/full/PiP presentation, local high-frequency state, and direct-to-routed playback fallback.

Skill-specific human-readable errors and translations stay in each skill. The scenario references `runtime.media_center.ui.*` keys with English fallbacks while `media_center_skill` owns both EN and RU skill resources. Only scenario identity text, such as the localized `Media Center` title, is packaged with the scenario itself.

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

The harness reports one-time FTS/trigram and folder-index backfill separately
from p50/p95/max catalog FTS, cursor-page, Home, root-folder, leaf-folder and
local-discovery latency, encoded page bytes, process RSS, sample counts and the
exact enforced budgets. Correctness assertions reject empty FTS/fuzzy results,
incomplete search indexes and invalid page sizes. The same run migrates and
removes 20,000 legacy works and collections, verifies 20,000 contextual
works/memberships, and enforces a bounded migration budget. It uses generated
descriptors and never needs private media bytes.
