# ReDevice Voice

Debug skill for validating ReDevice as a low-level voice endpoint.

The current iteration uses push-to-talk capture on the endpoint and member-side processing:

- ReDevice captures microphone audio only while the on-screen button or hardware fallback is held.
- The endpoint emits `endpoint.audio.record_button` and `endpoint.audio.segment` events.
- The skill stores the received WAV segment in skill runtime data.
- If VOSK and a compatible model are available on the member, the skill transcribes the segment.
- Recognized text is routed to the normal AdaOS NLU path through `nlp.intent.detect.rasa`.

The endpoint does not run local STT or TTS. VOSK is treated as a member-side capability. If VOSK is unavailable, the skill still validates microphone policy, push-to-talk events, and audio segment delivery.
