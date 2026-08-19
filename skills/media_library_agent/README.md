# Media Library Agent

`media_library_agent` is the node-local data-plane component of Media Center. It owns media roots, asynchronous filesystem discovery, reference registration, durable scan jobs, folder navigation, and ordered source deltas. It does not own the global catalog, grouping, personalization, playback sessions, or deployment policy.

## Storage boundary

Media bytes remain at their original paths. The agent calls `adaos.sdk.io.media.register_media_file`, which records an allowlisted reference for range playback; it never copies the source into `.adaos`. Removing or draining the skill retains external media by design.

## Scan model

- `import_folder` and `start_scan` return in seconds with durable job identifiers.
- One background scan runs per agent process. Duplicate requests for a root resolve to the active job.
- Jobs survive process restart. The service requeues interrupted work and incremental fingerprints avoid re-registering unchanged files.
- Progress is persisted and published as the bounded replace-mode stream variable `media_library_agent.current_scan` at no more than 2 Hz.
- `set_resource_pressure(playback)` pauses scanning so playback retains priority.
- Images are excluded unless enabled for a root. Symlinks are not followed by default; exclusions and periodic reconciliation are root policy.

## Coordinator contract

The coordinator pulls `adaos.media_library.source_delta.v1` records with an opaque cursor. Deltas are ordered per agent, source revisions are monotonic, and replay is idempotent. Folder segments are included in source metadata so search remains useful for numbered audiobook and album tracks.

## Distributed topology adapter

The exported `distributed_topology_phase` tool implements the public core
adapter ABI. Phase receipts are durable and keyed by the core idempotency key.
External-root moves fail closed when the selected node does not own the root;
no topology phase copies original media. Read admission, promotion, demotion,
drain, and removal publish revisioned replica observations through
`adaos.sdk.distributed`. Removing a replica retains the configured root and its
external files.
