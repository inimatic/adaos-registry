# ReDevice Settings

Service skill for the `redevice_user_face` scenario.

The skill is the operator-facing settings surface for ReDevice endpoints. It is
not the owner of endpoint infrastructure. It consumes the Endpoint Registry
read model through `sdk.data.devices` and sends endpoint commands through
`sdk.data.device_access`.

## Scope

MVP responsibilities:

- list ReDevice endpoints from the Endpoint Registry;
- select one endpoint as the current settings target;
- show online state, trust level, active app, active surface, manifest, policy,
  diagnostics, and service state;
- rename the endpoint and update aliases;
- keep a scenario-level current assignment until this moves into core
  `EndpointAssignment` storage;
- send policy-bound settings commands such as Wi-Fi settings, Bluetooth
  settings, speaker test, diagnostics, and logout;
- connect scenario users to `slideshow_skill` and `redevice_voice`.

## Boundary

`redevice_settings` is intentionally a skill. The AdaOS browser client should
only render its `webui.json`. ReDevice-specific command logic belongs in SDK and
Endpoint Registry/Router surfaces, not in the client.

Current command delivery still bridges to the legacy ReDevice root API. The
stable skill-facing API is already endpoint-oriented so the transport can move
behind `EndpointRouter` later.

## Data Route

`redevice_settings.state` is a compact replace stream. It is limited to table
rows, selected endpoint summary, small status sections, LAN admission rows, and
a bounded `last_command` acknowledgement. Full manifest, policy, diagnostics,
and large command responses must stay out of the stream and be requested through
explicit inspect/detail tools.

Subscription churn must not republish the full state. The stream uses
`webio.stream.snapshot.requested` for first paint; `subscription.changed` is only
an observation signal.

The main endpoint table intentionally includes both `endpoint_id` and pair code
so multiple identical Android devices can be distinguished before the operator
renames them.

