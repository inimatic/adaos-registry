"""Node maintenance presentation; Core retains all operation authority."""
from adaos.sdk import maintenance


def snapshot(section, target_node_id):
    result = maintenance.snapshot(section)
    result["target_node_id"] = target_node_id
    for job in result.get("migrations", []):
        job["target_node_id"] = target_node_id
    for location in result.get("locations", []):
        measured = location.get("measured_bytes")
        location["measured_display"] = "Not measured" if measured is None else f"{measured / 1024**3:.2f} GiB"
        location["display_path"] = location["active_path"]
        location["proposed_path"] = location.get("desired_path") or location["active_path"]
    for item in result.get("volumes", []):
        item["display_free"] = f"{item['free_bytes'] / 1024**3:.1f} GiB"
        item["display_total"] = f"{item['total_bytes'] / 1024**3:.1f} GiB"
    return result


def plan_location(target_node_id, **kwargs):
    return {**maintenance.plan_location(**kwargs), "target_node_id": target_node_id,
            "requested_path": kwargs["desired_path"]}


def apply_location(target_node_id, plan_digest):
    return {**maintenance.apply_location(plan_digest), "target_node_id": target_node_id}
