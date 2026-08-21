# Media Center Project

`media_center` is the distribution boundary for the Media Center product. It
ships one scenario, the catalog coordinator, node-local library agent, and the
playback control plane as an exact compatibility set. Media Server is a shared
dependency and remains the owner of core media resource registration and byte
delivery.

Release `0.6.45` locks the distributed coordinator, node-local agent, persistent
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
`media_library_agent@0.6.20`, `media_center_skill@0.8.40`, and scenario
`0.6.8` as one compatibility closure.

The `0.6.45` release requires core `0.1.917` and adds the coordinator-side
preflight for versioned rolling-release overlap. Topology status exposes the
active `ServiceDefinition` v2 through the public distributed SDK, and an
upgrade that omits the current exact release is rejected before any Media
Center topology mutation. Physical two-node rolling acceptance remains a
separate recorded gate.

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
