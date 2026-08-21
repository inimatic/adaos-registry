# Media Center Coordinator

`media_center_skill` is the logical Media Center coordinator. It owns the global catalog read model, work/variant/collection identity, profile-scoped favorites and resume state, bounded search, and playback planning. It does not walk filesystems or own source media.

`media_library_agent` owns node-local roots and asynchronous scans. The coordinator invokes its public tools and consumes ordered `adaos.media_library.source_delta.v1` pages. The agent registers each source through `adaos.sdk.io.media.register_media_file`; only a root-bound reference is stored in `.adaos`, while media bytes stay at their original path.

## Catalog

`library()` uses deterministic FTS over normalized title, filename, relative path, meaningful folder segments, tags, people, aliases, and collection names. Queries execute explicitly when the tool is called. FTS first selects a bounded 192-record window by default, then applies profile policy and the versioned `deterministic-fts-v2` lexical order to a narrow id/rank page before materializing full rows. `MEDIA_CENTER_SEARCH_CANDIDATE_LIMIT` can tune the window from 64 to 10,000; responses expose the effective limit, candidate count, and truncation signal, and never claim an exact total for a bounded search. Exact and rare terms still match against the complete FTS index before the window is applied.

`deep_search()` keeps FTS first, then adds a bounded local discovery stage with transliterated phonetic, trigram, and 48-dimension local text-embedding signals, followed by bounded agents for technical and not-yet-materialized shard fields. Every stage preserves profile policy, reports partial participants, and exposes ranking evidence. The trigram index admits at most 5,000 candidates by default (20,000 hard maximum) and fully scores the best 600 by default (5,000 hard maximum). Catalog pages use opaque versioned cursors and are clamped to 30 rows. Non-search continuation uses keyset anchors for stable latency as the catalog grows; bounded FTS continuation uses its opaque offset inside the fixed candidate window. Both paths fetch one extra row instead of performing an unbounded exact count, and `total_count_exact=false` exposes that truth. Legacy offset cursors remain readable for rolling upgrades. The compact player projection remains clamped to ten entries, while durable playback sessions can request a bounded queue of up to 500 entries. Images are excluded from the default `playable` view.

The coordinator derives explicit `MediaSource`, `MediaVariant`, `MediaWork`, `MediaCollection`, and membership rows. Deterministic grouping preserves series/season/episode, album/disc/track, audiobook/part/chapter, and source-folder levels. Folder navigation is a separate lazy, cursor-backed read model with breadcrumbs. Duplicate results are review candidates only and never authorize source deletion. Metadata, merge, split, and regroup corrections are audited and reversible; alias activation/revocation remains explicit evidence.

Provisional audio work identity includes normalized folder/collection context,
so files named only `0.mp3` or `01.mp3` in different books cannot become
playback alternatives by filename alone. Matching contextual tracks from
different agents may still become variants. A variant id belongs to its exact
physical source and remains stable when migration, enrichment, or a reviewed
correction reclassifies that source under another work.

Profile-owned playlists have revision-safe ordered membership and explicit `private`, `household`, or `shared` visibility. Playlist reads remain cursor-backed and catalog/player limits remain independent; deleting a playlist never deletes source media.

## Playback Planning

`playback_plan()` deterministically selects one available variant for the target endpoint. The decision considers an explicit user override, advertised codecs, maximum video height and bitrate, quality/language preferences, and source/endpoint co-location. Its result includes bounded decision evidence rather than hiding policy inside the client.

Every plan carries an `adaos.media_center.playback_route.v1` contract. A source-agent direct URL is preferred when available; the plan always describes a root-routed HTTP fallback tied to the source node. `ensure_rendition()` routes a browser-compatibility request to the exact source-owning agent. Completed derived resources are hidden variants of the same work and remain tied to the exact source revision/fingerprint. `build_playback_queue()` creates ordered snapshots from an item, work, collection, folder, or profile-visible playlist. It also returns the generic `adaos.playback.endpoint_control.v1` adapter descriptor that lets the app shell open and reconcile a durable endpoint session without embedding Media Center behavior in the client. Queue construction is bounded to 500 and never copies original media bytes.

Coordinator FTS rows share the owning `catalog_items.rowid`. Incremental agent
updates therefore replace search and fuzzy-search records by indexed row id
instead of scanning the full virtual table; schema migration rebuilds legacy
FTS rows once before publishing the new schema revision.

Delta ingestion queues durable enrichment jobs. A newer source revision cancels an older queued job for the same item and kind, retains only the eight newest terminal receipts, and leaves at most one queued metadata, embedding, and fingerprint job per subject. This keeps durable observability without allowing repeated scans to grow the queue or RSS without bound. One low-priority worker selects a versioned provider, records bounded `MetadataClaim` rows with provenance/confidence, and publishes terminal operation progress. The built-in deterministic provider uses only indexed filename, folder, tag, and technical evidence. Technical probes, exact/perceptual fingerprints, thumbnails, and embeddings use the same provider/job boundary; unavailable providers fail explicitly rather than blocking catalog reads or touching source bytes. Perceptual duplicate groups are review-only evidence: no candidate is automatically merged or deleted.

SQLite WAL mode is established during schema creation and migration, not renegotiated on every read connection. Read connections retain foreign-key enforcement, a 30-second busy timeout, and `synchronous=NORMAL`; this avoids WAL mode lock coordination in catalog, playback, and search hot paths while continuous agent deltas are being committed.

Agent availability is independent from known catalog identity. The coordinator discovers ready `skill:media_library_agent` service instances through `adaos.sdk.distributed`, invokes each exact instance through the public service boundary, and stores one cursor per instance/agent binding. Reads retain known rows but report `partial=true` when an expected instance is stale, missing, or unavailable. Every applied source revision advances a catalog revision, and replayed agent deltas are ignored idempotently.

The colocated compatibility path first reads the agent's stable identity and then resumes that agent's durable cursor. It never restarts delta ingestion at sequence zero merely because distributed topology is not configured, and it never borrows a cursor from a different prior local agent.

When an agent delta names the same contained source path as a legacy `media_server` compatibility row, the coordinator retires the legacy row from normal reads. This removes duplicate UI entries during migration without deleting the original file or its core media reference.

## Personal State

Favorites, recent playback, resume positions, completion, ratings, hidden items, playlists, and recommendations are keyed by `profile_id`. Catalog queries and playback plans enforce media-kind, maturity, explicit-content, hidden-item, and shared-screen history policy. Mutations publish the bounded replace-mode `media_center.library_state` stream with catalog and personal revisions, participation, home shelves, recent operations, and an explicit collection state. The state machine distinguishes loading, indexing, unconfigured, configured-but-empty, ready, updating, and unavailable from persisted coordinator and agent witnesses; an absent snapshot is never interpreted as an empty library. Multiple browsers can therefore converge without putting a full catalog into synchronized state. The stream supports snapshot replay after reconnect. Every snapshot echoes the subscribed `profile_id` and `shared_surface` values in stream metadata and uses a distinct variable identity, so personal and shared-screen projections cannot cross-deliver.

Recommendations evaluate at most three catalog pages, use only local favorite and history signals, include bounded machine-readable reasons, and can be disabled per profile. Profiles also own home-row order, default list/grid/rail view, density, and preferred playback target. `voice_request` is the structured entrypoint for search, collections, favorites, status, target discovery, playback, and transport control. It returns at most ten visual candidates, asks for clarification when media or targets are ambiguous, and delegates sessions and commands to `media_control_skill` so voice and direct controls share policy, leases, and revisions. A natural multi-step request is translated into a bounded `media.compound_control` workflow request with profile/target context, per-step idempotency and schedule constraints. It is never executed directly: the governed workflow owner must obtain confirmation and reconcile unknown effects.

`diagnostic_export()` combines bounded deployment, topology, scan, catalog/search, provider, playback, route, QoE, and optional browser observations. Paths, credentials, direct URLs, media bytes, and unbounded logs are excluded or redacted. Repair recommendations contain review-required tool plans; they never mutate deployment or data automatically.

Topology administration remains reviewed and SDK-only. `define_topology()` binds a service definition to the current desired ProjectRelease digest, verifies the definition/group identity and all optimistic revisions before any topology write, and rejects a component package digest as a release identity. `plan_topology_change()` persists an immutable dry-run plan, `apply_topology_change()` reads that plan through the SDK and applies its exact digest with idempotency and only the approvals declared by the plan, `topology_operation_status()` reads one durable operation by ID, and `handoff_authority()` performs an explicit revision- and epoch-fenced recovery handoff. Topology application has a 10-minute tool budget for large catalog snapshots; the durable core operation remains the source of truth and can resume an interrupted `pending` or `running` plan with the same idempotency key. Project deployment uses `adaos.sdk.deployment.submit`, returns the durable operation immediately, and exports `deployment_operation_status` for direct bounded progress reads; rollout duration is not coupled to an interactive tool timeout. The coordinator never imports the distributed runtime store or adapter implementation.

Distributed catalog catch-up is cursor-based and lifecycle-managed. The coordinator adapts an oversized agent page down to the core service-invocation envelope, keeps the accepted cursor unchanged while retrying, and continues incomplete catch-up in one single-flight background worker. The worker inherits the admitted skill execution context with `contextvars.copy_context()`, so SDK capability checks remain active off the invocation thread. `dispose()` stops both the sync and enrichment workers; UI reads only bootstrap synchronously when the coordinator catalog is empty.

## Compatibility

The local public-tool invocation and old Media Server discovery methods remain bounded compatibility fallbacks when no distributed agent instance is registered or the handler is exercised outside an AdaOS skill context. In a Project deployment, exact service-instance invocation is authoritative; root and scan commands return asynchronous job identifiers.

## User-Facing Errors

Tools return stable machine codes and may include `human_message_i18n`. Media Center translations live in this skill's `assets/i18n/*.json` resources, not in core, the scenario, or the client. Core browser-asset publication turns them into immutable content-addressed URLs during materialization. `human_message` remains a fallback for clients that have not loaded the runtime dictionary.
