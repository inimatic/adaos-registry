"""Bounded current Builder selection; never infer a destination from a project list."""
from adaos.sdk import access, applications


def get_current_builder_application():
    access.require("workspace.read")
    result = {
        "status": "unavailable", "reason": "Current Builder selection is unavailable",
        "title": "Unavailable", "stage": "Unavailable", "revision": "Unavailable",
        "observed_at": None, "freshness": "unavailable",
        "builder": {"available": False, "webspace_id": ""},
        "preview": {"available": False, "webspace_id": ""},
    }
    try:
        snapshot = applications.get_current_builder_application()
    except Exception:
        return result
    if not isinstance(snapshot, dict):
        return result
    status = snapshot.get("status")
    if status not in ("ready", "ambiguous", "unavailable"):
        return result
    result["status"] = status
    for key in ("reason", "observed_at"):
        value = snapshot.get(key)
        if isinstance(value, str):
            result[key] = value[:512]
    # Preserve the SDK freshness indicator, without synthesizing a timestamp.
    freshness = snapshot.get("freshness")
    if freshness in ("current", "unavailable"):
        result["freshness"] = freshness
    for key in ("builder", "preview"):
        destination = snapshot.get(key)
        if not isinstance(destination, dict):
            continue
        identity = destination.get("webspace_id")
        if destination.get("available") is True and isinstance(identity, str) and identity.strip() and len(identity) <= 512:
            result[key] = {"available": True, "webspace_id": identity}
    if status != "ready":
        reason = result["reason"]
        result["stage"] = reason
        result["title_i18n"] = {"key": f"web_desktop.development.builder.{reason}"}
        if reason == "builder_not_active":
            result["title"] = "No active Builder workspace"
        elif reason == "multiple_builder_hosts":
            result["title"] = "Choose a Builder workspace"
        elif reason == "builder_selection_unavailable":
            result["title"] = "No application selected in Builder"
        else:
            result["title"] = "Builder selection unavailable"
        result["revision"] = "—"
        return result
    application = snapshot.get("application")
    if isinstance(application, dict):
        for target, source in (("title", "title"), ("stage", "phase")):
            value = application.get(source)
            if isinstance(value, str) and value.strip():
                result[target] = value[:512]
        revision = application.get("revision")
        if type(revision) is int:
            result["revision"] = str(revision)[:512]
        elif isinstance(revision, str) and revision.strip():
            result["revision"] = revision[:512]
    else:
        result["stage"] = result["reason"] or "Unavailable"
    return result
