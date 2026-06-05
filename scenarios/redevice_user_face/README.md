# ReDevice User Face

Scenario for operator-facing ReDevice endpoint workflows.

The scenario installs and groups:

- `redevice_settings` for Endpoint Registry-backed settings and assignments;
- `slideshow_skill` for display endpoint validation and media projection;
- `redevice_voice` for microphone endpoint validation, PTT, and VAD debugging.

The scenario is intentionally thin. ReDevice-specific UI is provided by skills
through `webui.json`; core endpoint access remains behind SDK surfaces and the
Endpoint Registry.
