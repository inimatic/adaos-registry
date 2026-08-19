# Media Center Coordinator

`media_center_skill` is the logical Media Center coordinator. It owns the global catalog read model, work/variant/collection identity, profile-scoped favorites and resume state, bounded search, and playback planning. It does not walk filesystems or own source media.

`media_library_agent` owns node-local roots and asynchronous scans. The coordinator invokes its public tools and consumes ordered `adaos.media_library.source_delta.v1` pages. The agent registers each source through `adaos.sdk.io.media.register_media_file`; only a root-bound reference is stored in `.adaos`, while media bytes stay at their original path.

## Catalog

`library()` uses deterministic FTS over normalized title, filename, relative path, meaningful folder segments, tags, people, aliases, and collection names. Queries execute explicitly when the tool is called. Pages use opaque cursors and are clamped to 30 rows; the player queue remains clamped to ten. Images are excluded from the default `playable` view.

The coordinator derives explicit `MediaSource`, `MediaVariant`, `MediaWork`, `MediaCollection`, and membership rows. Current deterministic grouping covers series episodes, albums, audiobooks, and source folders. Duplicate results are review candidates only and never authorize source deletion. Enrichment, technical probes, fingerprints, thumbnails, and embeddings are represented as observable background jobs rather than blocking catalog reads.

Agent availability is independent from known catalog identity. Reads retain known rows but report `partial=true` when an expected agent is stale or unavailable. Every applied source revision advances a catalog revision, and replayed agent deltas are ignored idempotently.

## Personal State

Favorites, recent playback, resume positions, and completion are keyed by `profile_id`. Mutations return targeted invalidation tags so multiple browsers in one webspace can refresh the same profile projection without publishing a full catalog into synchronized state.

## Compatibility

The old root and Media Server discovery methods remain compatibility fallbacks when `media_library_agent` is not installed or the handler is exercised outside an AdaOS skill context. In a Project deployment, root and scan commands are delegated to the agent and return asynchronous job identifiers.

## User-Facing Errors

Tools return stable machine codes and may include `human_message_i18n`. Media Center translations live in this skill's `i18n/*.json` resources, not in core, the scenario, or the client. `human_message` remains a fallback for clients that have not loaded the runtime dictionary.
