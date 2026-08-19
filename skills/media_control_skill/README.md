# Media Control Skill

`media_control_skill` is the persistent control plane for Media Center playback. It owns target registration, session identity, revisioned queues and commands, control leases, checkpoints, per-profile settings, and bounded QoE evidence. It never proxies media bytes.

Controllers may live in a different webspace from the playback target. Commands are optimistic, idempotent, and lease-guarded. The endpoint pulls an ordered command stream and acknowledges application; stale controllers receive an explicit revision conflict. A phone therefore controls a TV without becoming part of the source-to-TV data path.

Queues support at most 500 durable entries and are read in pages of at most 30; compact player selectors request ten. `next` and autoplay skip unavailable members. Handoff changes the target and enters `recovering` until the new endpoint reconciles the session.

`autoplay` and `auto_fullscreen` default to true and are scoped by profile plus target. Browser background audio and video close policy are explicit settings. Native process-suspension guarantees remain deferred to Android Media3 and Apple AVAudioSession.

The app shell subscribes to `media_control.now_playing`, owns the media element, and stores high-frequency position locally. Durable checkpoints are coalesced at meaningful boundaries rather than synchronized on every `timeupdate`.
