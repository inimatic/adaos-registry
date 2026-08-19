# Media Center Coordinator

`media_center_skill` is the logical Media Center coordinator. It owns the global catalog read model, work/variant/collection identity, profile-scoped favorites and resume state, bounded search, and playback planning. It does not walk filesystems or own source media.

`media_library_agent` owns node-local roots and asynchronous scans. The coordinator invokes its public tools and consumes ordered `adaos.media_library.source_delta.v1` pages. The agent registers each source through `adaos.sdk.io.media.register_media_file`; only a root-bound reference is stored in `.adaos`, while media bytes stay at their original path.

## Catalog

`library()` uses deterministic FTS over normalized title, filename, relative path, meaningful folder segments, tags, people, aliases, and collection names. Queries execute explicitly when the tool is called. Pages use opaque cursors and are clamped to 30 rows; the player queue remains clamped to ten. Images are excluded from the default `playable` view.

The coordinator derives explicit `MediaSource`, `MediaVariant`, `MediaWork`, `MediaCollection`, and membership rows. Deterministic grouping preserves series/season/episode, album/disc/track, audiobook/part/chapter, and source-folder levels. Folder navigation is a separate lazy, cursor-backed read model with breadcrumbs. Duplicate results are review candidates only and never authorize source deletion. Metadata, merge, and regroup corrections are audited and reversible.

Profile-owned playlists have revision-safe ordered membership and explicit `private`, `household`, or `shared` visibility. Playlist reads remain cursor-backed and catalog/player limits remain independent; deleting a playlist never deletes source media. Enrichment, technical probes, fingerprints, thumbnails, and embeddings are represented as observable background jobs rather than blocking catalog reads.

Agent availability is independent from known catalog identity. The coordinator discovers ready `skill:media_library_agent` service instances through `adaos.sdk.distributed`, invokes each exact instance through the public service boundary, and stores one cursor per instance/agent binding. Reads retain known rows but report `partial=true` when an expected instance is stale, missing, or unavailable. Every applied source revision advances a catalog revision, and replayed agent deltas are ignored idempotently.

## Personal State

Favorites, recent playback, resume positions, and completion are keyed by `profile_id`. Mutations publish the bounded replace-mode `media_center.library_state` stream with catalog and personal revisions, participation, home shelves, and recent operations. Multiple browsers can therefore converge without putting a full catalog into synchronized state. The stream supports snapshot replay after reconnect.

## Compatibility

The local public-tool invocation and old Media Server discovery methods remain bounded compatibility fallbacks when no distributed agent instance is registered or the handler is exercised outside an AdaOS skill context. In a Project deployment, exact service-instance invocation is authoritative; root and scan commands return asynchronous job identifiers.

## User-Facing Errors

Tools return stable machine codes and may include `human_message_i18n`. Media Center translations live in this skill's `i18n/*.json` resources, not in core, the scenario, or the client. `human_message` remains a fallback for clients that have not loaded the runtime dictionary.
