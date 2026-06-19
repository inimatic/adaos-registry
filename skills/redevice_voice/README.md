# ReDevice Voice

Debug skill for validating ReDevice as a low-level voice endpoint.

The current iteration uses legacy-safe sound activation by default and keeps
push-to-talk as deterministic fallback:

- ReDevice runs simple VAD/sound activation with pre-roll, silence cutoff, and
  max segment duration.
- Push-to-talk captures microphone audio only while the on-screen button or
  hardware fallback is held.
- The endpoint emits `endpoint.audio.voice_activity`,
  `endpoint.audio.record_button`, `endpoint.audio.segment`, and optional
  `endpoint.audio.transcript` events.
- `EndpointAudioService` stores only the last 10 debug WAV segments.
- If VOSK and a compatible model are available on the member, the skill transcribes the segment.
- Recognized text is routed to the normal AdaOS NLU path through `nlp.intent.detect.rasa`.
- The `Check audio` action verifies the latest received audio segment as a
  content artifact: endpoint policy, selected transport, retained WAV file,
  sample metadata, and debug retention.

The endpoint does not require local STT or TTS. VOSK on Android is optional and
must degrade to member-side STT or segment diagnostics. If STT is unavailable,
the skill still validates microphone policy, activation events, retention, and
audio segment delivery.
