"""Core-governed updates; ingress owns routing and retry identity."""
from adaos.sdk import access, system


def _request_id():
    access.require("workspace.write")
    invocation = access.invocation()
    value = invocation.get("request_id") if isinstance(invocation, dict) else None
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        raise ValueError("invocation_request_id_missing")
    return value


def request_core_update():
    return system.request_core_update(request_id=_request_id())


def set_core_autoupdate(enabled):
    request_id = _request_id()
    if type(enabled) is not bool:
        raise ValueError("enabled_must_be_boolean")
    return system.set_core_autoupdate(request_id=request_id, enabled=enabled)
