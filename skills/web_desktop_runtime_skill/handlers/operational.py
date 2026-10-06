"""Inert display projections; absence is never interpreted as a measurement."""
from __future__ import annotations

import math


def record(value):
    return value if isinstance(value, dict) else {}


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def bytes_display(value):
    size = number(value)
    if size is None:
        return None
    units = ("B", "kB", "MB", "GB", "TB")
    amount = float(size)
    unit = units[0]
    for candidate in units:
        unit = candidate
        if abs(amount) < 1000 or candidate == units[-1]:
            break
        amount /= 1000
    precision = 0 if unit == "B" else 1
    return f"{amount:.{precision}f} {unit}"


def update_buttons(update):
    """Project only the transition controls already supported by Infra State."""
    if update.get("development") is True:
        return []
    state = str(update.get("state") or "").strip().lower()
    if state not in {"planned", "preparing", "countdown", "draining", "stopping"}:
        return []
    buttons = []
    reason = str(update.get("reason") or "").strip().lower()
    if reason.startswith(("github.push:", "root.release:")):
        buttons.append({"id": "refuse_update", "label": "Refuse update", "kind": "danger"})
    if state in {"planned", "countdown"}:
        buttons.extend([
            {"id": "defer_update_5m", "label": "Delay 5m", "kind": "secondary"},
            {"id": "defer_update_15m", "label": "Delay 15m", "kind": "secondary"},
        ])
    buttons.append({"id": "cancel_update", "label": "Cancel update", "kind": "danger"})
    return buttons


def project(snapshot, section):
    subject = record(snapshot.get("subject"))
    observed = snapshot.get("observed_at")
    result = {"observed_at": observed, "freshness": "unavailable"}
    if section == "subnet":
        subnet = record(snapshot.get("subnet"))
        members = record(snapshot.get("member_summary"))
        delivery = record(snapshot.get("development_delivery"))
        result.update(
            subnet_id=subnet.get("subnet_id") if subnet.get("available") is True else None,
            subnet_name=subnet.get("display_name") if subnet.get("available") is True else None,
            online_summary="Unavailable",
            delivery_status="Development reports unavailable",
            freshness=subnet.get("freshness", "unavailable"),
        )
        # Only explicitly available aggregates are suitable for operational copy.
        if members.get("available") is True:
            online, total = number(members.get("online")), number(members.get("total"))
            if online is not None and total is not None:
                result["online_summary"] = f"{online} of {total} online"
        if delivery.get("available") is True:
            delivered, accepted = number(delivery.get("delivered")), number(delivery.get("accepted"))
            if delivered is not None and accepted is not None:
                result["delivery_status"] = f"Development reports: {delivered} delivered, {accepted} accepted"
                result["delivery_status"] += " · " + str(delivery.get("freshness", "unavailable"))
                if delivery.get("last_delivery_at"):
                    result["delivery_status"] += " · " + str(delivery["last_delivery_at"])
    if section == "current_node":
        result["name"] = subject.get("display_name") or subject.get("name") or subject.get("title")
    if section in {"update", "incidents", "applications"}:
        result.update(value="Unavailable", label={"update": "Core update", "incidents": "Needs attention", "applications": "Installed in subnet"}[section], description="Current data unavailable", color="warning")
        if section == "update":
            update = record(snapshot.get("update"))
            channel = str(update.get("runtime_channel") or "").strip()
            version = str(update.get("runtime_version") or "").strip()
            release = " | ".join(value for value in (channel, version) if value)
            result.update(
                value=update.get("state") or "Unavailable",
                description=update.get("message") or "Update status unavailable",
                subtitle=release or observed or "Observation time unavailable",
                development=update.get("development") is True,
                buttons=update_buttons(update),
            )
        elif isinstance(snapshot.get(section), list):
            result.update(value=len(snapshot[section]), description="Observed records" + (f" · {observed}" if observed else " · observation time unavailable"))
    if section in {"incidents", "applications"} and isinstance(snapshot.get(section), list):
        result["description"] = "Observed records"
        result["subtitle"] = observed or "Observation time unavailable"
    if section == "metrics":
        resources = record(snapshot.get("resources"))
        freshness = resources.get("freshness", "unavailable")
        metrics = []
        for key, label in (("cpu", "CPU"), ("memory", "RAM"), ("disk", "Disk")):
            sample = record(resources.get(key))
            percent = number(sample.get("percent")) if resources.get("available") is True else None
            if percent is not None and percent > 100:
                percent = None
            display = f"{percent:g}%" if percent is not None else "Unavailable"
            used, total = number(sample.get("used_bytes")), number(sample.get("total_bytes"))
            used_display, total_display = bytes_display(used), bytes_display(total)
            if percent is not None and used_display and total_display:
                display += f" · {used_display} / {total_display}"
            metrics.append({"id": key, "label": label, "value": percent, "display": display, "description": freshness})
        cpu = metrics[0]
        result.update(
            hardware_metrics=metrics,
            center={
                "value": cpu["display"] if cpu["value"] is not None else "—",
                "label": "CPU" if cpu["value"] is not None else "CPU unavailable",
            },
            subtitle=resources.get("observed_at") or "Observation time unavailable",
            value=freshness,
            label=" · ".join(f"{m['label']} {m['display']}" for m in metrics),
            description="Last reported hardware telemetry",
            freshness=freshness,
        )
    return result
