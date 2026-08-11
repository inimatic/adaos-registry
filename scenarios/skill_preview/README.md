# Skill Preview

Shared read-only fallback presentation for a skill selected in Builder when no Project or skill manifest declares a more specific scenario presentation.

The scenario deliberately owns no domain state. It asks `builder_sdk_control_skill` for the selection of the paired Builder host and renders the skill metadata, capabilities, and README. Development authority remains in Builder.
