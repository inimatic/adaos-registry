# Media Center Project

`media_center` is the distribution boundary for the Media Center product. It
ships one scenario, the catalog coordinator, node-local library agent, and the
playback control plane as an exact compatibility set. Media Server is a shared
dependency and remains the owner of core media resource registration and byte
delivery.

Release `0.6.8` locks the distributed coordinator, node-local agent, persistent
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

The same release supports colocated one-node operation and selected-node or
capability-based deployment. Entry-point bindings select presentation
semantics (`desktop`, `tv`, `mobile_control`, or `embedded`); they do not infer
component placement from viewport width. Workspace-scoped playback and
settings modals remain independent from node-scoped diagnostics.

Uninstall removes unreferenced Project-owned code while retaining runtime
evidence and source artifacts. External media roots are never Project-owned,
never copied into `.adaos`, and never deleted by Project lifecycle operations.
Derived rendition retention remains a separate reviewed choice.
