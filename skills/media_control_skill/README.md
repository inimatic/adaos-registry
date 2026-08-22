# Media Control Skill

`media_control_skill` is the persistent control plane for Media Center playback. It owns target registration, session identity, revisioned queues and commands, control leases, checkpoints, per-profile settings, and bounded QoE evidence. It never proxies media bytes.

Version `0.2.4` is a provenance-only package revision for Project `0.6.48`;
runtime behavior is unchanged from `0.2.2`.

Controllers may live in a different webspace from the playback target. Commands are optimistic, idempotent, and lease-guarded. The endpoint pulls an ordered command stream and acknowledges application; stale controllers receive an explicit revision conflict. A phone therefore controls a TV without becoming part of the source-to-TV data path.

`open_endpoint_session()` is the endpoint admission boundary used by the app shell. It registers the browser/TV identity and creates the new queue session in one tool call while atomically retiring any older active session for that target. Command pulls can be scoped to the exact session, so a restarted endpoint never replays historical commands from a prior playback run.

Each session keeps coordinator, command, queue, and endpoint-observation revisions separate. `reconcile_endpoint()` accepts a monotonic endpoint revision, acknowledges applied command revisions, and returns exactly one `load`, `seek`, transport, command-replay, or no-op decision. Repeating the same observation replays the recorded decision and cannot issue a duplicate seek. Endpoint-preferred recovery preserves playback that continued during a control outage, including an autonomous next-item transition within the admitted queue; coordinator-preferred recovery restores a newer durable checkpoint.

Queues support at most 500 durable entries and are read in pages of at most 30; compact player selectors request ten. `next` and autoplay skip unavailable members. Handoff changes the target and enters `recovering` until the new endpoint reconciles the session.

`autoplay` and `auto_fullscreen` default to true and are scoped by profile plus target. Target settings inherit profile defaults until explicitly overridden. Browser background audio, video close policy, playback rate, language preferences, checkpoint cadence, track selection, and reconnect policy are explicit settings. Sleep timers become durable pause commands, so an endpoint that reconnects after the deadline still converges to paused state. Native process-suspension guarantees remain deferred to Android Media3 and Apple AVAudioSession.

The app shell owns the media element and consumes the generic `adaos.playback.endpoint_control.v1` descriptor returned with a queue. The descriptor names the control skill and its open, pull, and reconcile methods; the generic client does not embed a Media Center skill name. The shell publishes state changes and 15-second checkpoints, polls only the exact active session for revisioned commands, and applies each command id once. Controllers subscribe to `media_control.now_playing`; the skill implements both declared ready and snapshot-request subscriptions, so reconnect has a bounded state seed. High-frequency position remains local rather than entering UI/Yjs synchronization.

QoE accepts a fixed metric vocabulary for planning, first frame, seek, rebuffer, route changes, interruption, and completion. `qoe_summary()` returns bounded recent evidence plus count/average/maximum/total aggregates for a session or target; raw source paths and media bytes are excluded.
