# Media Control Skill

`media_control_skill` is the persistent control plane for Media Center playback. It owns target registration, session identity, revisioned queues and commands, control leases, checkpoints, per-profile settings, and bounded QoE evidence. It never proxies media bytes.

Controllers may live in a different webspace from the playback target. Commands are optimistic, idempotent, and lease-guarded. The endpoint pulls an ordered command stream and acknowledges application; stale controllers receive an explicit revision conflict. A phone therefore controls a TV without becoming part of the source-to-TV data path.

Each session keeps coordinator, command, queue, and endpoint-observation revisions separate. `reconcile_endpoint()` accepts a monotonic endpoint revision, acknowledges applied command revisions, and returns exactly one `load`, `seek`, transport, command-replay, or no-op decision. Repeating the same observation replays the recorded decision and cannot issue a duplicate seek. Endpoint-preferred recovery preserves playback that continued during a control outage; coordinator-preferred recovery restores a newer durable checkpoint.

Queues support at most 500 durable entries and are read in pages of at most 30; compact player selectors request ten. `next` and autoplay skip unavailable members. Handoff changes the target and enters `recovering` until the new endpoint reconciles the session.

`autoplay` and `auto_fullscreen` default to true and are scoped by profile plus target. Target settings inherit profile defaults until explicitly overridden. Browser background audio, video close policy, playback rate, language preferences, checkpoint cadence, track selection, and reconnect policy are explicit settings. Sleep timers become durable pause commands, so an endpoint that reconnects after the deadline still converges to paused state. Native process-suspension guarantees remain deferred to Android Media3 and Apple AVAudioSession.

The app shell subscribes to `media_control.now_playing`, owns the media element, and stores high-frequency position locally. The skill implements both declared ready and snapshot-request subscriptions, so reconnect has a bounded state seed. Durable checkpoints are coalesced at meaningful boundaries rather than synchronized on every `timeupdate`.

QoE accepts a fixed metric vocabulary for planning, first frame, seek, rebuffer, route changes, interruption, and completion. `qoe_summary()` returns bounded recent evidence plus count/average/maximum/total aggregates for a session or target; raw source paths and media bytes are excluded.
