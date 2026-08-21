# Media Center Project

`media_center` is the distribution boundary for the Media Center product. It
ships one scenario, the catalog coordinator, node-local library agent, and the
playback control plane as an exact compatibility set. Media Server is a shared
dependency and remains the owner of core media resource registration and byte
delivery.

Release `0.6.31` locks the distributed coordinator, node-local agent, persistent
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

Topology admission uses the enclosing ProjectRelease digest, not an individual
component package digest. The coordinator validates that identity against the
current desired deployment and checks group and dataset compare-and-switch
revisions before writing a service definition, group, or dataset. A mismatched
or stale topology request therefore fails before topology mutation.

Reviewed Project rollout is submitted as a durable background operation rather
than held inside an interactive skill RPC. The tool returns the operation id as
soon as core accepts it; bounded deployment status and direct operation status
then expose progress. Core serializes rollout work and resumes accepted/running
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
