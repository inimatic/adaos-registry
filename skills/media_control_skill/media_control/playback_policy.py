"""Confirmed endpoint observations are distinct from desired/resumed position."""
from typing import Any, Mapping


def playback_is_confirmed(endpoint_state: Mapping[str, Any], *, position_ms: int, state: str) -> bool:
    if int(endpoint_state.get("media_error_code") or 0) > 0:
        return False
    if "playback_confirmed" in endpoint_state:
        # Explicit false from a current endpoint outranks a restored checkpoint.
        return endpoint_state["playback_confirmed"] is True
    # Compatibility for explicit checkpoints from older endpoint adapters.
    # These are not a qualified substitute for the current acknowledgement path.
    return position_ms > 0 or state == "ended"
