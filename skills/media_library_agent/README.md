# Media Library Agent

`media_library_agent` is the node-local data-plane component of Media Center. It owns media roots, asynchronous filesystem discovery, reference registration, durable scan and rendition jobs, folder navigation, technical deep search, and ordered source deltas. It does not own the global catalog, grouping, personalization, playback sessions, or deployment policy.

Version `0.6.27` adds durable, bounded artwork backfill for sources that were
indexed before artwork extraction existed. Capability evidence makes terminal
`unavailable` results retryable when an FFmpeg backend later appears. Version
`0.6.28` emits video-frame JPEGs as full-range `yuvj420p`, which is required by
current FFmpeg MJPEG encoders and remains compatible with older supported
versions. Version `0.6.29` retries extraction at the start of a video when the
preferred five-second poster position is beyond a short clip, under the same
bounded timeout and output limits.

## Storage boundary

Original media bytes remain at their original paths. The agent calls `adaos.sdk.io.media.register_media_file`, which records an allowlisted reference for range playback; it never copies the source into `.adaos`. Removing or draining the skill retains external media by design. Browser-compatible renditions and normalized artwork are explicitly derived data: their exact source revision and fingerprint are recorded, and only generated outputs may be copied to managed media storage.

Process-local `.skill_state` is development/runtime data, not package source.
The workspace ignores it and the Project package builder excludes it, so a
release cannot embed a scanner database, local paths, or scan history.

The service declares `service.membership` instead of implementing a private
heartbeat. AdaOS binds membership to the exact active Project component,
registers a stable node/activation instance, renews its lease from the service
health projection, and expires stale memberships. The health endpoint exposes
only compact `distributed.health` and `distributed.pressure` fields to that
generic core supervisor.

Repository schema migration is serialized by a node-local process lock. Once
the recorded database revision and node binding are current, runtime handlers,
the migration worker, and the persistent service use a read-only fast path and
do not renegotiate SQLite WAL mode or acquire a writer lock during startup.
This keeps service health admission independent from concurrent core migration
and UI rehydration on slow storage. Worker transition tests use bounded
slow-storage deadlines rather than assuming a local SSD completes a scan in
two seconds.

The agent database and the Media Center coordinator database are deliberately
separate. This database is the node-local source of truth for roots, source
witnesses, technical facts, rendition jobs, and replayable changes. The
coordinator stores a compact federated read model for grouping, enrichment,
search, personalization, and playback planning; it must not become a second
copy of the complete agent descriptor.

Source search uses a contentless FTS5 index, so searchable JSON is tokenized but
is not retained a second time by the FTS content table. Full-state source deltas
remain ordered and idempotent, while old intermediate revisions are collapsed
after a one-hour grace period; the newest snapshot or tombstone for every source
is always retained, so a new or delayed coordinator can still rebuild its read
model. Descriptor fields represented by typed source columns or source metadata
are normalized at rest and reconstructed at the public contract boundary.

Logical compaction is resumable and runs in bounded worker batches. `status`
reports its phase, counters, SQLite free pages, and retained delta count.
`optimize_storage(reclaim=true)` is the explicit physical maintenance step: it
pauses new local work, waits for executing jobs, checkpoints WAL, verifies free
disk headroom, and rebuilds SQLite to return free pages to the filesystem.
Original media and generated rendition lifecycle rules are unchanged.

## Scan model

- `import_folder` and `start_scan` return in seconds with durable job identifiers.
- The persistent service process is the single owner of background work in production. Root-runtime tools only mutate the shared durable queue. Standalone development may opt into an in-process worker with `MEDIA_LIBRARY_AGENT_EMBEDDED_WORKER=1`.
- Jobs survive process restart. Only the service owner requeues interrupted work, preventing root/service fencing races; incremental fingerprints avoid re-registering unchanged files.
- Progress is persisted and published as the bounded replace-mode stream variable `media_library_agent.progress` at no more than 2 Hz. Snapshot replay includes the root label, and a terminal update retains the durable structured error, so a reconnecting Settings surface can distinguish queued, running, completed, and failed scans without polling or parsing logs.
- Terminal job transitions publish `media_library_agent.catalog.changed`, allowing the coordinator to pull bounded deltas without polling from the browser.
- `set_resource_pressure(playback)` persists pressure in the shared agent database. The service observes it within its one-second poll interval and pauses scanning or rendition work so playback retains priority.
- Images are excluded unless enabled for a root. Symlinks are not followed by default; exclusions and periodic reconciliation are root policy.
- A schedule can enable a bounded polling watcher. It fingerprints at most `MEDIA_LIBRARY_AGENT_WATCH_MAX_ENTRIES` filesystem entries, debounces bursts, queues an incremental scan after a change, and falls back to a full reconcile when the watch budget overflows. Periodic reconciliation remains authoritative after watcher or process interruption.
- Active roots may not overlap. Scan windows use node-local `HH:MM` times and weekday numbers (`0` is Monday); invalid windows fail closed.
- `MEDIA_LIBRARY_AGENT_MAX_BYTES_PER_SECOND` optionally throttles scanner read throughput. Progress reports phase, elapsed time, throughput, wait reason, and checkpoint age in a replace-mode variable.
- Every changed source receives a cheap basic technical descriptor. `MEDIA_LIBRARY_AGENT_PROBE_MODE=ffprobe` enables a bounded external probe when `ffprobe` is installed; `MEDIA_LIBRARY_AGENT_PROBE_TIMEOUT_SECONDS` is clamped to 1-30 seconds.
- Audio tags are read locally with Mutagen and normalized into title, artists, album, album artist, genres, date/year, track/disc, language, and MusicBrainz identifiers. A revisioned bounded rescan backfills sources indexed before this extractor existed; original bytes and arbitrary tags never leave the source node.
- `MEDIA_LIBRARY_AGENT_PERCEPTUAL_HASH_MODE=ffmpeg` optionally hashes a standardized bounded audio/video sample. It is off by default, uses one thread, emits at most 512 KiB, and has a 10-second default/30-second hard timeout. Only the hash enters catalog claims; sampled bytes are discarded and original files are unchanged.

## Rendition model

- `plan_rendition` compares source facts with explicit endpoint codecs, MIME types, containers, height, and bitrate. Compatible sources do not create a job.
- One rendition shares the agent's single background worker with scanning. Playback/critical pressure pauses it. CPU threads, RSS, timeout, output size, temporary disk quota, cancellation, and final publication are bounded by `MEDIA_LIBRARY_AGENT_RENDITION_*` settings.
- The worker writes a `.partial` temporary file, atomically closes it, rechecks the source witness, publishes the derived resource, and atomically advertises it in a new source delta. A source change before or after publication invalidates the job and removes the generated resource.
- `media_library_agent.rendition_progress` publishes the latest durable job at a bounded rate. Restart recovery requeues interrupted work; queued cancellation is immediately terminal.
- Artwork uses the same durable queue with the low-priority `artwork-card-v1` profile. The agent tries embedded tags, a bounded folder-cover lookup, then scores up to three deterministic video fragments and rejects near-black or otherwise uninformative frames. It emits the best maximum 720x1080 JPEG of at most 4 MiB with selection, provider, and exact-source evidence; missing backends or artwork fail as observable jobs instead of blocking scans.
- Existing sources are inspected through a durable cursor and a small bounded queue. Pillow, Mutagen, and a packaged `imageio-ffmpeg` fallback make local artwork extraction independent of host packages; `MEDIA_LIBRARY_AGENT_FFMPEG_PATH` may select an operator-managed binary instead. Backfill progress, capability evidence, and source counts by artwork state are exposed by `status`.
- Queued scans run before automatic artwork work. An unchanged rescan preserves ready artwork and does not enqueue a duplicate; a changed source fingerprint or folder-cover witness invalidates the corresponding projection.
- `search_sources` uses node-local Unicode FTS over names, folders, embedded metadata, and technical probe fields. Coordinator search remains the fast first stage; this endpoint is a bounded federated deep-search stage.

## Coordinator contract

The coordinator pulls `adaos.media_library.source_delta.v1` records with an opaque cursor. Deltas are ordered per agent, source revisions are monotonic, and replay is idempotent. Every page also carries a compact authoritative library-state witness with root, source, available-source, active-job, and failed-job counts. A rolling-upgrade coordinator treats a page from an older agent as unknown state instead of inferring that the library is unconfigured. Folder segments are included in source metadata so search remains useful for numbered audiobook and album tracks. Derived rendition descriptors remain variants of the existing work and never become duplicate catalog rows. Node-local folder navigation is separately cursor-backed and never materializes an entire tree.

## Distributed topology adapter

The exported `distributed_topology_phase` tool implements the public core
adapter ABI. Phase receipts are durable and keyed by the core idempotency key.
External-root moves fail closed when the selected node does not own the root;
no topology phase copies original media. Read admission, promotion, demotion,
drain, and removal publish revisioned replica observations through
`adaos.sdk.distributed`. Removing a replica retains the configured root and its
external files. Replicated catalog phases preserve the last verified checkpoint,
item count, byte count, and source witness when no external root is involved;
promotion and demotion therefore cannot erase the data evidence used for
fencing and recovery decisions. A follower reports its persisted transfer as
Replica evidence without changing the authoritative Partition checkpoint. An
authority derives both witnesses from its local catalog or, after promotion,
from the persisted replica snapshot. Authority handoff therefore cannot compare
a caller-supplied partition witness with verified replica state, and an empty
follower cannot regress the canonical checkpoint.

Read activation and promotion fail closed until the target publishes the
source checkpoint and item witness. Small catalog-state snapshots may use the
authenticated, digest-verified 64 KiB inline path. Larger snapshots are
serialized as a compressed derived-catalog artifact and transferred through
the core `distributed.topology.transfer` data plane in at most 96 KiB chunks.
The receiver persists resumable offsets, validates the compressed SHA-256
digest and logical catalog witness, and atomically replaces partition replica
metadata only after the complete item count is present. Transfer staging and
replica tables contain metadata only; original media bytes are never part of a
replica snapshot.

Repository identity comes from `adaos.sdk.core.runtime_identity()`. Existing
repositories that only contain the former `local` placeholder are migrated once
in place, including ordered delta payloads; media paths, root IDs, source IDs,
and registered external references are unchanged. Opening the same repository
under a different concrete node identity is rejected.
