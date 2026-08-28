# Media Center

Media Center is a Project-composed household media application. Its scenario is UI-as-data only; domain identity, indexing, playback, control, and byte delivery remain in skills, app shell, and core boundaries.

Version `0.6.29` keeps a path-centric folder browser and adds the app-shell endpoint inbox required for remote playback.
Folders and direct media share one cursor-backed page, but declarative typed
selection sends a folder into drill-down and sends only a media item to the
player. Breadcrumbs navigate back without materializing a directory tree.

## Surfaces

The adaptive UI-as-data toolbar opens Filter and sort through the generic left drawer presentation. It navigates server-provided genre and year facets, applies a minimum rating, and exposes title/date/rating/duration/progress/play-count/resolution/bitrate/random orders for list, grid, and carousel layouts. On a narrow surface the same overlay becomes full width without a Media Center-specific client component.

The Project entrypoints declare explicit `desktop`, `tv`, `mobile_control`, and `embedded` presentation profiles independently of viewport width. The client selects the profile from `surfaceProfile` (or the `presentation_profile` entrypoint query), applies stable density/overscan/input semantics, and keeps the same catalog and control contracts. TV uses content rails and D-pad focus; mobile control puts Now Playing, target selection, and transport first while retaining Browse/Search below them.

The main page follows familiar media-center navigation: Home, Movies, Series, Music, Audiobooks, Folders, Playlists, Favorites, and Recent. A single adaptive UI-as-data toolbar combines Remote, profile, section, layout, and Settings without media-specific client code. Its native buttons and menus work with pointer, touch, keyboard, and TV D-pad input; the mobile-control profile keeps its transport-first surface. Search is explicit-submit and has an explicit Reset action that clears both the query field and authoritative catalog filter. A submitted global query also clears stale collection, folder, genre, year, rating, and content-rating constraints, so a valid title cannot be hidden by a previous browsing context. Every catalog read uses opaque server cursors with 30 records per page. Selecting a series, album, or audiobook opens a bounded collection browser with breadcrumbs, child seasons/parts, representative artwork, and explicit Play All; selecting it never materializes an unbounded queue in the page. The universal `ui.list` renderer switches between list, grid, and carousel layouts, uses browser rendering virtualization, and provides deterministic spatial keyboard/D-pad focus. Carousel arrows move one bounded viewport while retaining focus on the arrow, so repeated TV-remote clicks remain stable.

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

The persistent app-shell coordinator owns the complete bounded queue, so Ended,
Next, Previous, Media Session actions, and server reconnect recovery continue
to work when the modal is absent. Reopening details from the mini-player first
restores the active queue item into page state, preventing the modal from
jumping back to the episode or track that originally opened the queue. The
player transport exposes Previous, Play/Pause, Next, Stop, Fullscreen,
capability-gated Picture-in-Picture, and Play on. The fullscreen overlay uses
the same controls and remains above buffering/recovery diagnostics. On narrow
touch surfaces toolbar labels collapse to accessible icon controls.

The content-first details surface is a fullscreen UI-as-data modal surface, not
native video fullscreen. Its split layout keeps the universal `item.details`
media presentation in the primary area and profile plus transport controls in
a bounded auxiliary area; mobile stacks the same areas. The details projection
contains a bounded poster/cover, primary metadata, quality, source node, safe
library-relative path, and available original/derived versions. Favorite,
Add to playlist, Edit metadata, and Close to mini-player form one peer action
row in the modal's non-scrolling docked footer. Add to playlist supports both existing profile-owned playlists and an
atomic create-and-add flow. Edit metadata shows the immutable source identity,
accepts reviewed title/overview/year/genre/artist/album/series values, and can
reject an incorrect TMDb or MusicBrainz match. Corrections are audited and
reversible in the coordinator; the scenario never edits source files or NFOs.

Media Center modals are workspace-scoped UI-as-data surfaces. The client therefore does not stamp the currently displayed node onto coordinator reads or actions; source-node selection remains an explicit playback-plan concern. This keeps the same modal valid on desktop, TV, controller, and federated-node views.

The Remote modal is a controller surface for another webspace or TV. The page-level `adaos.playback.endpoint_provider.v1` descriptor keeps the app-shell endpoint registered while idle, wakes it through `media_control.playback.assigned`, and recovers missed events through a 15-second inbox heartbeat. The returned queue window is capped at 30 entries around the active item. Locally opening playback still admits the browser/TV through the queue's generic control adapter. The app shell publishes meaningful state changes and bounded checkpoints, consumes session-scoped revisioned commands without overlapping pulls, and survives closing the player modal. A command is acknowledged only after the exact expected item and state are observed; Play additionally requires confirmed media output. Remote presents a compact target selector with device, endpoint, authorization and availability context, a human-titled Now Playing row for that target, and one transport toolbar. The same target selector is placed after Settings in the primary adaptive toolbar, so its variable device label cannot shift the Settings control. When that toolbar has a selected endpoint, the main player resolves its declarative remote-play action from page state and routes the actual Play gesture through `media_center_skill.play_on`; it neither starts the controller's media element nor requests controller fullscreen. The explicit Play-on modal uses the same backend operation. The selector refreshes options on open, expires every endpoint after a bounded heartbeat interval, and joins core device-registry presence when the endpoint declares a `device_ref`; stale browser sessions therefore do not accumulate in the menu. It subscribes to an immediately seeded, mutation-maintained `media_control.now_playing` projection and sends revision-safe transport intents through `media_control_skill`; a phone does not enter the media byte path. `media_control_skill` also contributes a standalone desktop icon and compact remote widget whose target and Now Playing status follow the same contracts.

Settings contains library roots, scan/import operations, profile/access and home-layout policy, playback defaults, agent resource/watch status, QoE, sanitized diagnostics, repair recommendations, and distributed deployment administration. Its universal details surface subscribes to `media_library_agent.progress`, so a queued or running scan shows the current root and bounded counters and a failed scan shows the durable diagnostic; root rows retain their last scan timestamp and status. Metadata and artwork activity are secondary and open in a dedicated modal; only that modal subscribes to the bounded `media_center.operation_state` and durable rendition-progress streams. It also reads the bounded rendition history, showing source, status, profile, produced bytes, path and terminal diagnostic, while the latest operation receives live byte updates. The same projection reports per-kind enrichment admission windows, provider activity including retry delay, provider-artwork cache counters, agent job-retention progress, SQLite allocation/reclaimable bytes, and resumable logical-compaction progress. They remain observable without adding background subscriptions to the main Settings surface, rebuilding home shelves, or putting job history into synchronized page state. Autoplay and auto-fullscreen use the generic `input.toggle` UI-as-data contract and read their authoritative profile values from `media_control_skill`; they are not mirrored in synchronized page state. Selecting a catalog object only opens details. The actual Play gesture starts media and requests native fullscreen when policy allows, so browser activation is used at the correct boundary instead of prearming fullscreen during selection. The app-shell media element remains stable through slow loads, resume and modal close. Native video controls stay disabled; modal and shell fullscreen surfaces retain AdaOS transport controls, including Previous and Next, and remain above recovery diagnostics. A failed media-element load exits native/schema fullscreen, records a bounded endpoint-local compatibility verdict, and presents Retry, Convert when a derived browser version is applicable, and Cancel over the player. Convert queues an exact-source-bound background rendition through `media_center_skill.ensure_rendition`; it never overwrites or relocates the original. `Play on` resolves the selected target's capability profile before queue planning and refuses an incompatible selected item until a ready rendition exists. Its first controller-target approval becomes a core-owned durable grant; changing either side requires a distinct approval. The player modal uses a split UI-as-data layout whose footer actions stay outside the scrolling content. Playback target selectors persist their visible fallback endpoint before actions become enabled, so `Play on` cannot submit an empty target merely because the first option was implicit. Operators create a reviewed all-matching placement plan, explicitly apply its digest, and separately drain or remove an activation through public deployment tools. Folder import and scan calls have a ten-minute client budget and return asynchronous agent jobs when the distributed agent is active. Images remain disabled as catalog media by policy; normalized artwork is a derived presentation resource resolved by the app shell through AdaOS media transport. Original media bytes stay at their source paths; `.adaos` stores references, catalog state, jobs, playback state, and explicitly generated derived renditions only.

When a ready node-local artwork route has become unavailable, the generic image
renderer advances through the bounded external provider candidates published
with the same artwork descriptor. This is a display fallback, not a second
catalog authority; source-local embedded, folder, generated, or cached artwork
remains preferred.

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
python scenarios/media_center/benchmarks/run_library_benchmark.py --count 50000 --enforce
python scenarios/media_center/benchmarks/run_library_benchmark.py --count 200000 --enforce
```

The sustained catalog/playback acceptance gate uses a real agent-delta writer
beside bounded FTS, cursor-page and playback-plan readers. Acceptance mode
cannot be shortened below one hour or reduced below 50,000 catalog items. The
fixture generator streams rows into SQLite and accepts up to 200,000 items, so
the acceptance harness itself does not materialize the catalog in process memory:

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

Read connections use a 32 MiB SQLite page cache and a lazily populated 256 MiB
memory-map window by default. Operators can tune these independently with
`MEDIA_CENTER_SQLITE_CACHE_MB` (8-256) and `MEDIA_CENTER_SQLITE_MMAP_MB`
(0-1024). Neither setting materializes the catalog or reserves its maximum
address range as resident memory.

The 2026-08-26 Windows development gates passed at both 50,000 and 200,000
catalog items. At 200,000 items, p95 was 130.630 ms for FTS, 35.105 ms for
cursor pages, 113.563 ms for Home, 11.879 ms for root folders, 3.956 ms for
leaf folders, and 385.059 ms for fuzzy discovery. RSS was 37.82 MiB. The
one-time search and metadata projection backfills took 91.541 and 72.368
seconds; the bounded 50,000-item identity migration took 26.888 seconds. At
50,000 items, FTS p95 was 71.038 ms and RSS was 35.254 MiB.

The 2026-08-26 Windows server acceptance run passed for 3,600.031 seconds with
50,000 catalog items and 289,875 applied agent deltas. It recorded no operation
errors; p95 was 80.441 ms for FTS, 73.586 ms for catalog pages, 38.874 ms for
playback plans, and 142.322 ms for delta application. RSS peaked at 40.523 MiB
with 0.668 MiB sustained growth, aggregate CPU p95 was 15.633%, and the final
WAL size was zero.

The matching one-hour production-bundle desktop renderer run stayed within all
resource budgets while the server workload ran: idle CPU was 4.498%, steady
main-thread CPU 5.473%, renderer private-memory p95 201.914 MiB, JS heap growth
2.46 MiB, DOM mutation rate 1.407/s, input-delay p95 13.3 ms, and dropped-frame
ratio zero. Eight long tasks were observed (0.133/min, maximum 832 ms). The
first harness result remained formally failed because its playback selector
looked for an English `Music` label in the Russian UI and treated an expected,
handled reliability-projection `503` as a browser error. The fixed harness uses
stable command IDs, accepts that fallback only beside a healthy node status,
and supports an explicit browser-compatible fixture. Its follow-up playback
probe advanced 29.883 seconds with media error code zero and preserved exactly
one media element after modal-to-mini transition. This closes the local desktop
performance investigation, not the separate one-hour physical Android TV gate.

The 2026-08-27 development-bundle `media-home` investigation made generic
`ui.list` card projections stable, enabled `OnPush`, and retained an existing
stream subscription when a materialization cycle supplied an equivalent widget
descriptor. Across comparable 45-second local probes, DOM mutation rate fell
from the previously observed 300-359/s range through 25.979/s to 6.655/s;
`media-home` disappeared from the final mutation targets. Final steady main
thread CPU was 6.096%, renderer CPU was 7.318%, heap growth was -0.204 MiB, and
no Long Tasks were observed. The development renderer still exceeded the
strict 5% idle process-CPU gate; production-bundle and physical Android TV
acceptance remain authoritative.

The harness reports one-time FTS/trigram and folder-index backfill separately
from p50/p95/max catalog FTS, cursor-page, Home, root-folder, leaf-folder and
local-discovery latency, encoded page bytes, process RSS, sample counts and the
exact enforced budgets. Correctness assertions reject empty FTS/fuzzy results,
incomplete search indexes and invalid page sizes. The same run migrates and
removes 20,000 legacy works and collections, verifies 20,000 contextual
works/memberships, and enforces a bounded migration budget. It uses generated
descriptors and never needs private media bytes.
