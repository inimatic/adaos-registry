# Media Center Project

`media_center` is the distribution boundary for the Media Center product. It
ships one scenario, the catalog coordinator, node-local library agent, and the
playback control plane as an exact compatibility set. Media Server is a shared
dependency and remains the owner of core media resource registration and byte
delivery.

Release `0.6.71` removes catalog startup work from the Hub event loop. It ships
`media_center_skill@0.8.62` and keeps the remaining `0.6.70` component set.
The runtime bootstrap is process-owned, retryable, observable, and drained on
slot disposal. An uncertain schema read now fails transiently instead of being
misclassified as permission for a full migration, while terminal-job pruning
runs only in bounded background batches. This closes the `.30` failure where
`sys.ready` held one handler for 240.7 seconds and the first library-agent
process then exceeded its 300-second health deadline.

Release `0.6.70` bounds catalog status reads on large and contended libraries.
It ships `media_center_skill@0.8.61` and keeps the remaining `0.6.69` component
set. Status, diagnostics, and collection-state streams reuse one compact
covering-index projection instead of scanning wide catalog rows. Exact byte
aggregation remains available as an explicit heavyweight administrative path;
the interactive projection reports that media bytes remain at their source.

Release `0.6.69` makes collection totals reflect available logical works. It
ships `media_center_skill@0.8.60` and keeps the remaining `0.6.68` component
set. Offline compatibility-agent records and multiple playable renditions stay
durable, but no longer duplicate series, season, or album counts in the UI.

Release `0.6.68` makes large-catalog identity reads sequential. It ships
`media_center_skill@0.8.59` and keeps the remaining `0.6.67` component set.
Repair scans explicitly avoid the selective media-kind index and unnecessary
temporary ordering: one sequential catalog pass replaces thousands of random
row lookups on slow disks.

Release `0.6.67` completes the bounded activation contract. It ships
`media_center_skill@0.8.58` and keeps the remaining `0.6.66` component set.
The lifecycle receipt reads only the scalar catalog revision; legacy full-table
summary and facet projections are published later by the long-lived runtime.

Release `0.6.66` republishes the `0.6.65` component set with its immutable
source pinned by the complete 40-character registry revision.

Release `0.6.65` makes the identity migration scale with library size. It ships
`media_center_skill@0.8.57` and keeps the remaining `0.6.64` component set.
Exact-source variants now have a dedicated lookup index, and migration/removal
updates use separate indexed source and exact-source statements. This removes
the repeated full-table scans observed while repairing a 68,000-item catalog.

Release `0.6.64` makes activation bounded on slow library storage. It ships
`media_center_skill@0.8.56` and keeps the remaining `0.6.63` component set.
The lifecycle `rehydrate` hook now validates durable catalog state without
starting process-local catalog sync or enrichment workers. Those workers and
their UI snapshot publications start once, in the long-lived Hub runtime, on
`sys.ready`. This prevents activation-time catch-up and enrichment from
contending with schema migration and control queries against the same SQLite
catalog. Coordinator schema revision `2026-08-24.3` also makes the series
identity revision-2 repair part of the activation gate instead of allowing the
schema fast path to skip it.

Release `0.6.63` is the identity and durable-worker follow-up. It ships
`media_center_skill@0.8.55`, `media_library_agent@0.6.33`,
`media_control_skill@0.2.10`, scenario `media_center@0.6.20`, and client
`0.0.373`. Episode identity now comes from the deterministic basename prefix
before `SxxEyy`, independent of optional imports. Incremental artwork updates
replace variant memberships atomically, and migration revision 2 removes
stale duplicate memberships and orphaned provisional collections. Claimed
scan/rendition exceptions become durable terminal diagnostics instead of
leaving invisible `running` jobs. An online backup of the real local catalog
migrated to one 6-item Black Mirror series and one 92-item MLP series, both
with representative artwork.

Release `0.6.62` is the locally verified collection and playback-quality
closure. It ships `media_center_skill@0.8.54`,
`media_library_agent@0.6.32`, `media_control_skill@0.2.10`, scenario
`media_center@0.6.20`, and client `0.0.373`. Collection cards inherit a ready
representative artwork resource from their members. Generated video artwork
uses the `informative-frame-v2` recipe: up to three deterministic samples are
scored for entropy, contrast, luminance, and clipping before the best frame is
published. Episodic filenames are parsed into a stable series identity, so
season folder labels no longer split one show into several series.

Playback history now requires decoded media data or an advancing media
position. Browser-incompatible AVI is rejected before opening a playback
session, leaves fullscreen with a durable retry diagnostic, and does not create
a new Recent entry. The app-shell mini-player exposes separate fullscreen and
Picture-in-Picture commands; platforms without usable PiP retain the fullscreen
path. Local acceptance covered Black Mirror artwork regeneration, unified MLP
season grouping, collection artwork projection, successful fullscreen and
mini-player playback, and terminal AVI failure.

Release `0.6.61` is the locally verified single-node closure. It ships
`media_center_skill@0.8.53`, `media_library_agent@0.6.31`,
`media_control_skill@0.2.9`, scenario `media_center@0.6.19`, and client
`0.0.371`. A monotonic per-profile projection revision makes Favorites and
Recent converge across browsers, while bounded home caching, limit-first
collection queries, membership-first artwork lookup, and coalesced playback
checkpoints keep large home libraries responsive. The client resolves the
application identity from the scenario-owned localized page schema. Local
acceptance covered the 3716-file UNC library: its full scan retained a durable
3721-file, 1.96-TB job receipt; indexed sizes remain nonzero, completed playback
appears in Recent, terminal sessions leave Remote, and artwork reaches the DOM
as an object URL produced by the AdaOS media plane rather than a direct
page-origin file request.

Release `0.6.60` was not promoted. Its immutable local receipt references a
source revision that still tracked process-local agent state, although the
package builder excluded that directory from component bytes. `0.6.61` is the
first release whose source revision and package boundary both exclude it.

Release `0.6.59` closes single-node scan, playback-failure, history, and artwork
transport gaps. It ships `media_center_skill@0.8.52`,
`media_library_agent@0.6.30`, `media_control_skill@0.2.9`, scenario
`media_center@0.6.18`, and client `0.0.370`. Settings receives an immediate
queued scan state followed by bounded durable progress and terminal diagnostics.
Playback queues preserve source sizes; unsupported sources leave fullscreen and
retain an endpoint-local retry verdict. Explicit mini-player close reconciles
`stopped`, endpoint observations update profile Recent, and artwork is resolved
from a sanitized resource descriptor through direct or authenticated routed
AdaOS media transport. The scenario title now owns its EN/RU identity text.

Release `0.6.58` is the locally accepted playback and controller convergence
closure. It ships `media_center_skill@0.8.51`,
`media_library_agent@0.6.29`, `media_control_skill@0.2.8`, and scenario
`media_center@0.6.17`, with client `0.0.369`. Search now has an explicit Reset
that clears the authoritative query. Effective autoplay and auto-fullscreen
settings travel with the playback plan; video enters stable modal fullscreen
and requests native browser fullscreen. Media Remote consumes an immediately
seeded, mutation-maintained profile/target projection instead of waiting for a
coincidental refresh. Device choices expose device, endpoint, authorization,
kind and availability through skill-owned EN/RU data fields and the client's
generic dynamic-option i18n resolver.

Release `0.6.57` was not promoted: its local release receipt referenced a
superseded source commit. The immutable receipt remains retained for audit, and
`0.6.58` is the first deployable release of this component set.

Release `0.6.56` is the locally accepted single-node product closure. It ships
`media_center_skill@0.8.50`, `media_library_agent@0.6.29`,
`media_control_skill@0.2.7`, and scenario `media_center@0.6.16`. Root browsing
is restricted to configured agent-owned roots and every drill-down/queue keeps
agent plus root identity, so legacy Media Server rows stay searchable without
flattening the folder tree. Metadata and artwork run as bounded observable jobs
at the storage node; embedded art and representative frames produce derived
JPEGs while original video/audio bytes remain external references. The
optional TMDb provider is explicit, paced, privacy-bounded, and disabled
without credentials. Library stream sequencing composes catalog and personal
revisions so a file scan, favorite, Recent, or profile change cannot be rejected
as stale merely because the other revision plane is ahead. Skill-localized
remote assets, ordered collection playback, compact controls, app-shell
mini-player Close, and rail navigation complete the browser compatibility set.

Release `0.6.55` locks the distributed coordinator, node-local agent, persistent
control plane, adaptive desktop/TV/controller presentation, federated search,
exact-source renditions, and bounded operations diagnostics as one immutable
Project closure. Membership, leases, desired topology, and observed replicas are
owned by the coordinator's authority plane; node agents validate local evidence
and return bounded receipts without creating divergent local topology stores. It
adds reviewed topology plan/apply/handoff tools and preserves replicated catalog
witnesses across adapter promotion and demotion. The ordinary
Project release builder resolves and stores all five selected packages,
including the shared Media Server dependency, before a reviewed deployment can
target nodes. This release also adds keyset catalog continuation, bounded
late-materialized FTS paging, and coalesced enrichment receipts so a 20,000-item
library remains bounded under concurrent indexing. Agent activation binds its
durable repository to the canonical SDK node identity. A one-time migration
rewrites legacy `local` root, source, and delta identities in place while
preserving root/source identifiers and external media references; a database
already bound to another concrete node fails closed.

The `0.6.55` release carries the `0.6.54` product closure with
`media_library_agent@0.6.28`. Its video-frame extractor explicitly requests a
full-range JPEG pixel format, verified against the packaged FFmpeg 7.1 binary,
so automatic posters do not become terminal failures on current runtimes.

The `0.6.54` release completes the single-node metadata and collection path.
`media_library_agent@0.6.27` resumes a bounded durable artwork backfill and
extracts embedded or representative video frames at the storage node without
copying source media. `media_center_skill@0.8.48` exposes the pipeline state,
supports an explicitly enabled and privacy-bounded TMDb provider, projects
safe artwork URLs, and browses albums, audiobooks, series, seasons, and parts
in natural track or episode order through 30-item cursor pages. The collection
detail surface enters child collections instead of opening an arbitrary file.
`media_control_skill@0.2.6` supplies one compact remote surface with target,
now-playing state, and Previous/Play-Pause/Next/Stop controls. Scenario
`media_center@0.6.15` consumes these contracts through UI-as-data.

The `0.6.53` release polishes the colocated single-node product surface.
Carousel arrows now move bounded rails on pointer and mobile layouts; typed
Home selection enters folders while playable items open the player. The
app-shell media element survives modal dismissal in a mini-player with separate
Stop and Close commands. The compact remote groups target selection, human
Now Playing data, and transport controls, and is also available as a
skill-owned desktop surface. Metadata operation streams mount only in their
dedicated modal. Reference-only Media Server catalog rows are normalized into
the queue route contract without copying source bytes. This closure ships
`media_center_skill@0.8.46`, `media_control_skill@0.2.5`, and scenario
`media_center@0.6.14`.

The `0.6.52` release hardens the large-library browser path. The coordinator
does not wake its agent worker for ordinary reads and publishes a library
snapshot only when the bounded sync summary changes. Continue, Recent, and
Favorites start from indexed profile state instead of sorting the complete
catalog. A transactionally maintained metadata-only folder projection provides
folders-first, cursor-backed drill-down with breadcrumbs and direct files;
source bytes remain at their original paths. On the 20,000-item local gate,
`home` p95 was 227.967 ms, root folders 14.280 ms, and a 30-file leaf page
28.246 ms. Scenario `0.6.13` uses typed UI-as-data selection so folder clicks
navigate and media clicks open playback.

The `0.6.51` release makes root removal visible to the distributed catalog
without deleting source media. `media_library_agent@0.6.26` atomically disables
the root, requests cancellation of active scans, marks its present sources as
missing, and emits ordered `removed` deltas. Repeating the operation is
idempotent. Re-adding the same root can restore the retained source evidence,
while ordinary catalog reads stop presenting files from a disabled root.

The `0.6.50` release adds validated reuse of an unchanged catalog transfer
snapshot in `media_library_agent@0.6.25`. Before reuse, the agent compares the
exact catalog witness and checkpoint, validates the complete zlib stream, and
checks the declared item count. A changed or corrupt snapshot is ignored and
rebuilt. New snapshots use a latency-oriented compression level. This avoids
repeatedly scanning and expensively recompressing the same large catalog during
authority handoff while retaining metadata-only transfer and direct references
to media at its original storage location.

`0.6.49` remained a local prepublication build. Stand validation exposed a
ten-minute cold snapshot timeout, so its source closure was not published and
`0.6.50` received a fresh immutable release identity after the bounded
compression correction.

The `0.6.41` release adds an authoritative endpoint-session bridge between the
app-shell media element and `media_control_skill`. Playback admission retires a
prior active session for the same endpoint, commands are pulled for the exact
session and acknowledged by monotonic endpoint observations, and autonomous
queue advance is accepted under endpoint-preferred recovery. Remote controllers
therefore observe the TV's durable Now Playing projection without entering the
media byte path. The queue exposes a generic adapter descriptor, keeping Media
Center method names out of the reusable client service.

The `0.6.42` release compacts the browse surface into one adaptive UI-as-data
toolbar for Remote, profile, section, layout, and Settings. The generic client
renderer owns input behavior; Media Center labels, options, and actions remain
scenario-owned.

The `0.6.43` release localizes the complete static Media Center surface in
English and Russian. The scenario carries only stable key references and
English fallbacks; dictionaries remain versioned assets of
`media_center_skill@0.8.39`.

The `0.6.44` release adds the node-local artwork pipeline. Embedded tags,
bounded folder covers, and bounded video frames are normalized into a derived
JPEG with exact-source evidence; originals are never copied or modified. The
coordinator exposes a sanitized versioned artwork projection and Settings
observes the durable agent job stream. It ships
`media_library_agent@0.6.20`, `media_center_skill@0.8.41`, and scenario
`0.6.8` as one compatibility closure.

The `0.6.45` release requires core `0.1.917` and adds the coordinator-side
preflight for versioned rolling-release overlap. Topology status exposes the
active `ServiceDefinition` v2 through the public distributed SDK, and an
upgrade that omits the current exact release is rejected before any Media
Center topology mutation. Physical two-node rolling acceptance remains a
separate recorded gate.

The `0.6.46` release keeps the accepted `0.6.45` topology and upgrades
`media_center_skill` to `0.8.42`. Compact diagnostics and explicit search-index
refresh now derive their row count from the ordinary catalog table's strict
one-to-one rowid invariant. They never execute `COUNT(*)` against the FTS5
virtual table, which previously could hold a large-library status request for
minutes while reading the complete token payload. Scenario `0.6.9`, control
skill `0.2.2`, library agent `0.6.21`, and Media Server `0.9.16` allocate new
immutable package identities for the new Project source revision even though
their runtime behavior is unchanged.

The `0.6.48` release separates high-rate metadata progress from catalog and
personal projections. `media_center_skill@0.8.44` publishes bounded operation
state through its own replayable stream, builds a full library snapshot only
when enrichment settles, adds queue claim/recent indexes, recovers stale
running jobs, and prunes terminal receipts in bounded batches. This removes
the sustained full-home rebuild observed on the 68,000-item `.30` library.
Scenario `0.6.12` consumes the new operation stream. Control skill `0.2.4`,
library agent `0.6.23`, and Media Server `0.9.18` are provenance-only package
revisions for the release closure.

`0.6.47` remained a local prepublication build. Its source revision changed
when the final composition documentation was corrected, so immutable release
admission rejected replacement and `0.6.48` received fresh package identities.

The exact `0.6.46` release was subsequently rolled across both `.30` nodes by
deployment revision `50` and operation
`deploymentop.01M0MHYGHXNCXVRA772C4E2G5Z`. Exact-only topology definition v24
and generation `22` report both stable instances ready with `partial=false`.
Against 68,429 catalog rows and a 1.1 GiB coordinator database, compact status
completed in 0.803 seconds and exact filename search in 0.165 seconds. Range
playback remained `206 audio/mpeg`, the source witness was unchanged, and the
agent continued to report external-reference storage with no copied media
bytes. Android TV interaction and the one-hour browser soak remain separate
acceptance gates.

The `0.6.40` release added authoritative persisted playback toggles through the
generic client `input.toggle` contract and explicit subscription-backed library
collection states. `media_library_agent@0.6.19` publishes compact configuration
and scan witnesses with every delta page; `media_center_skill@0.8.38` persists
those observations and does not mistake an older agent's absent witness for an
empty configuration. The prior `0.6.38` closure republished its agent so every target
node prepares a runtime slot bound to the exact Project package manifest digest.
This prevents a same-version historical slot from satisfying a newer package
activation and makes a declared but undiscovered service fail deployment health.

Catalog replica observation derives its checkpoint, item count, byte count, and
source reference from node-local repository evidence instead of trusting a
caller's witness. A follower reports its persisted transferred snapshot only
as Replica state and cannot replace the canonical authority checkpoint. After
promotion, an authority with no local roots can recover that checkpoint from
the same persisted snapshot. Re-observing an unchanged partition reuses its
current revision while the coordinator advances only the replica revision with
compare-and-switch. This prevents an empty follower from regressing a live
partition and preserves verified metadata across activation restart.

Topology admission uses the enclosing ProjectRelease digest, not an individual
component package digest. The coordinator validates that identity against the
current desired deployment and checks group and dataset compare-and-switch
revisions before writing a service definition, group, or dataset. A mismatched
or stale topology request therefore fails before topology mutation.

Reviewed Project rollout is submitted as a durable background operation rather
than held inside an interactive skill RPC. The tool returns the operation id as
soon as core accepts it; bounded deployment status and direct operation status
then expose progress. Direct operation status is part of the exported skill
contract, so clients do not need access to core-private deployment storage.
Core serializes rollout work and resumes accepted/running
operations after restart from their immutable authorization record.

Coordinator rehydration restores process-owned workers and publishes the
available durable snapshot without synchronously draining agent deltas inside
the activation transaction. Cursor catch-up continues in the observable
background worker one bounded page at a time. Worker disposal reports whether
the process-owned threads actually stopped, so a later slot switch fails closed
instead of overlapping an unbounded writer with the new runtime.

Bounded coordinator FTS applies profile, media-kind, source, collection, and
availability filters before the candidate window is cut. A broad folder or
filename match therefore cannot be filled by a different media kind before the
requested audio or video rows are considered.

The node-local agent closes every SQLite connection at the repository context
boundary. Folder navigation uses a covering root/presence/folder index, keeping
the grouping query bounded on large libraries and avoiding leaked readers that
can delay scans, search, activation, or schema migration.

The core-process lifecycle no longer opens the repository owned by the agent
service. Rehydrate and dispose return explicit deferred receipts; schema
migration and worker recovery run only after the service supervisor switches
the active slot. This prevents an old service reader from overlapping a new
slot's migration on large or slow databases.

Active node agents declare their distributed membership contract in the skill
manifest. The core runtime owns registration, lease renewal, stale-member
expiration, and reconciliation against the exact active Project release. Health
responses expose only bounded pressure and availability evidence; they do not
mutate topology. Newly activated skill handlers replace the previous deployment
in the running process, so a successful Project apply does not require a hub
restart before its tools and streams become authoritative.

Topology apply reads the immutable reviewed plan through the public SDK and
grants only the approvals named by that plan. Ordinary replica creation carries
no authority or removal approval; handoff and removal remain separately fenced.
Route diagnostics now select one of the declared Media Center datasets and use
`media-catalog-authority` by default; the former undeclared placeholder dataset
is rejected before it reaches the generic distributed runtime.

The one-hour concurrent acceptance gate keeps the static 150 ms catalog-search
target separate from its 200 ms FTS target under continuous agent deltas. RSS
leak detection compares bounded post-warmup baseline and terminal windows while
still reporting the full high-water range and enforcing the 350 MiB absolute
limit.

The 2026-08-21 local server run passed that full gate for 3,600.063 seconds on
20,000 items with 307,950 concurrent agent deltas, zero errors, 64.079 ms FTS
p95, 39.02 MiB peak RSS, 0.793 MiB sustained RSS growth, and 13.533% aggregate
CPU p95. Exact two-node deployment is now recorded through `0.6.46`; Android
TV and update-under-playback evidence remain separate acceptance gates.

When a complete topology-backed agent sync supersedes colocated compatibility
mode, the coordinator retires unbound compatibility participation and marks its
old source projections inactive. Cleanup is deferred while any distributed
agent still has pages to deliver, so the active catalog never exposes a partial
migration as complete and source media remains untouched.

The same release supports colocated one-node operation and selected-node or
capability-based deployment. Entry-point bindings select presentation
semantics (`desktop`, `tv`, `mobile_control`, or `embedded`); they do not infer
component placement from viewport width. Workspace-scoped playback and
settings modals remain independent from node-scoped diagnostics.

Uninstall removes unreferenced Project-owned code while retaining runtime
evidence and source artifacts. External media roots are never Project-owned,
never copied into `.adaos`, and never deleted by Project lifecycle operations.
Derived rendition retention remains a separate reviewed choice.
