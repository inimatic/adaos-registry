# ReDevice User Face Roadmap

## MVP

- Use Endpoint Registry as the source of device identity and online state.
- Keep ReDevice Settings as the service surface for rename, aliases, status,
  policy-bound actions, logout, revoke, and retire.
- Connect settings to Slideshow and ReDevice Voice without embedding their
  specifics in the browser client.
- Keep detailed diagnostics in modal surfaces to avoid oversized dashboards.

## Next

- Move assignment storage from skill memory into core EndpointAssignment.
- Add stable SDK helpers for active app, endpoint command status, and settings
  command capabilities.
- Add Bluetooth output diagnostics and best-effort pairing guidance.
- Replace legacy ReDevice command bridge with EndpointRouter transport
  selection.
