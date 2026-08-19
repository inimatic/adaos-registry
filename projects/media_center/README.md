# Media Center Project

`media_center` is the distribution boundary for the Media Center product. It
ships one scenario, the catalog coordinator, node-local library agent, and the
playback control plane as an exact compatibility set. Media Server is a shared
dependency and remains the owner of core media resource registration and byte
delivery.

The same release supports colocated one-node operation and selected-node
deployment. Entry-point bindings select presentation semantics (`desktop`,
`tv`, `mobile_control`, or `embedded`); they do not infer component placement
from viewport width.

Uninstall removes unreferenced Project-owned code while retaining runtime
evidence and source artifacts. External media roots are never Project-owned,
never copied into `.adaos`, and never deleted by Project lifecycle operations.
