"""Governed member-node mutations; Core ingress owns member routing."""

from adaos.sdk import access, system


def rename_selected_node(display_name):
    access.require("workspace.write")
    name = str(display_name or "").strip()
    if not name:
        raise ValueError("display_name_required")
    return system.rename_current_node(name)
