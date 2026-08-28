# Media Control Skill

`media_control_skill` is the persistent control plane for Media Center playback. It owns target registration, session identity, revisioned queues and commands, control leases, checkpoints, per-profile settings, and bounded QoE evidence. It never proxies media bytes. A new endpoint session starts as `requested`; endpoint observations then publish the actual `loading`, `buffering`, `playing`, `paused`, `stopped`, or `failed` state. An undecoded browser load is not projected as playing, so Remote and resource-pressure decisions consume endpoint-owned facts.

Controllers may live in a different webspace from the playback target. Commands are optimistic, idempotent, and lease-guarded. The endpoint pulls an ordered command stream and acknowledges application; stale controllers receive an explicit revision conflict. A phone therefore controls a TV without becoming part of the source-to-TV data path.

Each command carries the expected active item, desired transport state, command
revision, and queue revision. Reconciliation accepts only a contiguous
acknowledgement prefix that matches the endpoint's observed item and state;
Play additionally requires media-element-confirmed playback. Wrong-item or
premature acknowledgements remain pending for bounded replay. Manual Previous
and Next preserve transport intent: a playing/requested session continues from
the beginning of the new item, while paused or stopped sessions only change the
selection.

The skill owns its controller surfaces as well as the control-plane methods. Its
Web UI contribution installs a Media Remote desktop app and a compact transport
widget. The modal combines one playback-device selector, one bounded Now
Playing projection, and one transport row. The single Play/Pause intent is
resolved against the current revision into an ordinary `play` or `pause`
command, so endpoints receive no ambiguous transport operation. Media Center
can consume the same methods and stream without owning or duplicating the
desktop contribution.

The compact widget also owns a target dropdown and a subscribed Now Playing
status row. Its stream receives an immediate exact-scope snapshot and later
authoritative mutation events; it does not poll browser-local playback state.
Its dynamic target options refresh when the selector opens, so expired
endpoints disappear without materializing an unbounded browser history.

`open_endpoint_session()` is the endpoint admission boundary used by the app shell for locally initiated playback. `endpoint_inbox()` is the complementary idle boundary: it refreshes endpoint presence and returns the latest nonterminal assignment with at most 30 queue entries around the active item. A terminal assignment remains visible only while its command revision is newer than the endpoint acknowledgement; this guarantees delivery of `stop` before the session leaves the inbox. Remote session creation, queue replacement, and commands publish a targeted `media_control.playback.assigned` wake-up event; the inbox heartbeat is the seeded recovery path when an event is missed. Command pulls remain scoped to the exact session, so a restarted endpoint never replays historical commands from a prior playback run.

Each session keeps coordinator, command, queue, and endpoint-observation revisions separate. `reconcile_endpoint()` accepts a monotonic endpoint revision, acknowledges applied command revisions, and returns exactly one `load`, `seek`, transport, command-replay, or no-op decision. Repeating the same observation replays the recorded decision and cannot issue a duplicate seek. Endpoint-preferred recovery preserves playback that continued during a control outage, including an autonomous next-item transition within the admitted queue; coordinator-preferred recovery restores a newer durable checkpoint.

Queues support at most 500 durable entries and are read in pages of at most 30; compact player selectors request ten. `next` and autoplay skip unavailable members. Handoff changes the target and enters `recovering` until the new endpoint reconciles the session.

`autoplay` and `auto_fullscreen` default to true and are scoped by profile plus target. Target settings inherit profile defaults until explicitly overridden. Browser background audio, video close policy, playback rate, language preferences, checkpoint cadence, track selection, and reconnect policy are explicit settings. Sleep timers become durable pause commands, so an endpoint that reconnects after the deadline still converges to paused state. Native process-suspension guarantees remain deferred to Android Media3 and Apple AVAudioSession. Endpoint observations distinguish an opened session from media-element-confirmed playback; a source that fails before its first `play`/`playing` event is not projected into profile history.

The app shell owns the media element and consumes the generic `adaos.playback.endpoint_control.v1` queue descriptor plus the page-level `adaos.playback.endpoint_provider.v1` UI-as-data descriptor. The descriptors name the control skill and its inbox, open, pull, and reconcile methods; the generic client does not embed a Media Center skill name. The shell registers and listens while idle, publishes state changes and 15-second checkpoints, permits only one in-flight command pull per endpoint session, and applies each command id once. Explicitly closing the shell mini-player reconciles a terminal `stopped` observation before releasing the binding; closing only the player modal retains the live source and session. Every durable session mutation publishes a best-effort `media_control.playback.observed` event for profile-history consumers, while the session database remains authoritative if that projection transport is temporarily unavailable. Controllers subscribe to `media_control.now_playing`; the skill immediately seeds each exact webspace/profile/target subscription, remembers a bounded active-projection set, and republishes those projections after every authoritative session mutation. Snapshot requests and rehydrate remain recovery paths rather than the ordinary freshness mechanism. High-frequency position remains local rather than entering UI/Yjs synchronization. Targets whose endpoint heartbeat is older than 60 seconds are reported unavailable and cannot receive a new session until they register again, including legacy targets created before presence metadata was introduced. When an endpoint capability carries a core `device_ref`, the skill also reads `adaos.sdk.data.devices.get_device_presence()`; authoritative core-offline state excludes the target even if a stale service heartbeat says otherwise. Core remains the reusable device-liveness owner, while this skill owns only playback-specific admission and capability evidence.

`now_playing()` joins only control-plane records already stored with the
session. Its read model excludes terminal sessions and sessions whose endpoint
heartbeat is older than the bounded freshness window; durable session records
remain available for explicit recovery and diagnostics. It exposes the queue title, target device name, endpoint name,
authorization state, target kind, media kind, and artwork descriptor next to
the revisioned session state. Controllers therefore show a human title and a
recognizable device/endpoint identity rather than opaque identifiers, and do
not query the catalog or move media bytes. EN/RU labels for the app, widget,
modal, and authorization state are packaged in this skill's `webui.json`
resources. Core publishes those dictionaries through immutable browser-asset
URLs without owning Media Control wording.

QoE accepts a fixed metric vocabulary for planning, first frame, seek, rebuffer, route changes, interruption, and completion. `qoe_summary()` returns bounded recent evidence plus count/average/maximum/total aggregates for a session or target; raw source paths and media bytes are excluded.
