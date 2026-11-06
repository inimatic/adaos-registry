"""Public description for the Context Chat application service."""
from adaos.sdk.core.decorators import tool


@tool(summary='Describe the private Context Chat service.', side_effects='read_only')
def describe():
    return {'ok': True, 'application_id': 'context_chat', 'service': 'context_chat_service',
            'features': ['projects', 'text_chat', 'document_context', 'editable_memory', 'chatgpt_oauth'],
            'account_required_for_inference': True, 'credentials_exposed': False}
