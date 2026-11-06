"""Permission declarations and consumer delegation, not Core authorization proof."""
from pathlib import Path

import pytest
import yaml

from test_behavior_contract import consumer


@pytest.mark.parametrize("operation,args,capability", [
    ("get_output_routes", (), "workspace.read"),
    ("request_output_routes", (), "workspace.write"),
    ("select_output_routes", (["opaque"], "stored"), "workspace.write"),
])
def test_core_denial_precedes_conversation_access(consumer, operation, args, capability):
    consumer.sdk_access.require.side_effect = PermissionError("Core denied caller")
    with pytest.raises(PermissionError):
        getattr(consumer, operation)(*args)
    consumer.sdk_access.require.assert_called_once_with(capability)
    consumer.sdk_conversation.communication_context.assert_not_called()
