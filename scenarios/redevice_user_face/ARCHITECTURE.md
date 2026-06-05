# ReDevice User Face Architecture

ReDevice User Face is a scenario, not a new device subsystem. It composes
service skills around the Endpoint Registry so future user-authored skills can
address ReDevice endpoints through the same SDK surface.

## Boundaries

- Endpoint identity, online status, trust, policy, and active app state are
  registry concepts.
- Skills use `adaos.sdk.data.devices` and `adaos.sdk.data.device_access`.
- ReDevice Settings owns the operator settings surface.
- Slideshow and Voice own scenario-specific media and audio behavior.
- The browser client renders declarative WebUI and must not contain
  ReDevice-specific command logic.

## Data Flow

1. Endpoint Registry exposes ReDevice inventory and runtime snapshots.
2. ReDevice Settings publishes a compact `redevice_settings.state` WebIO stream.
3. The settings surface selects the active endpoint and assignment.
4. Slideshow and Voice skills consume the selected endpoint when the user opens
   their surfaces or starts an action.
5. Endpoint commands are routed through SDK wrappers and later through
   EndpointRouter transport selection.
