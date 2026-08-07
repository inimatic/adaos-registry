"""Provider-neutral tracker contract and durable local reference provider."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence

from research.contracts import canonical_json, digest, identity, now
from research.repository import ResearchRepository


FINAL_STATUSES = {"succeeded", "failed", "cancelled", "lost"}
SPLIT_ROLES = {"train", "validation", "robustness", "test", "system"}
VALUE_TYPES = {"float", "integer", "boolean", "string", "vector", "table", "distribution"}


class TrackerConflict(ValueError):
    """The same idempotency identity was reused with different content."""


@dataclass(frozen=True, slots=True)
class TrackerDescriptor:
    provider_id: str
    contract_version: str
    capabilities: tuple[str, ...]
    limits: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "adaos.research.tracker_descriptor.v1",
            "provider_id": self.provider_id,
            "contract_version": self.contract_version,
            "capabilities": list(self.capabilities),
            "limits": dict(self.limits),
        }


class TrackerProvider(Protocol):
    descriptor: TrackerDescriptor

    def health(self) -> Mapping[str, Any]: ...

    def open_session(self, **values: Any) -> Mapping[str, Any]: ...

    def append_observations(
        self, session_id: str, observations: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, Any]: ...

    def append_artifacts(
        self, session_id: str, artifacts: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, Any]: ...

    def close_session(
        self, session_id: str, status: str, completeness: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def export_session(self, session_id: str) -> Mapping[str, Any]: ...


def observation_event_id(session_id: str, payload: Mapping[str, Any]) -> str:
    metric = dict(payload.get("metric") or {})
    step = dict(payload.get("step") or {})
    producer = dict(payload.get("producer") or {})
    coordinate = {
        "session_id": session_id,
        "namespace": metric.get("namespace"),
        "name": metric.get("name"),
        "split_role": payload.get("split_role"),
        "dataset_digest": payload.get("dataset_digest"),
        "step": step,
        "aggregation": payload.get("aggregation"),
        "producer_sequence": producer.get("sequence"),
    }
    return identity("tracker_event", coordinate)


def normalize_observation(
    session: Mapping[str, Any], value: Mapping[str, Any]
) -> dict[str, Any]:
    payload = dict(value)
    metric = dict(payload.get("metric") or {})
    namespace = str(metric.get("namespace") or "experiment").strip()
    name = str(metric.get("name") or "").strip()
    if not name:
        raise ValueError("observation metric.name is required")
    split_role = str(payload.get("split_role") or "system")
    if split_role not in SPLIT_ROLES:
        raise ValueError(f"unsupported observation split role: {split_role}")
    value_type = str(payload.get("value_type") or "float")
    if value_type not in VALUE_TYPES:
        raise ValueError(f"unsupported observation value type: {value_type}")
    if value_type in {"float", "integer"} and isinstance(payload.get("value"), bool):
        raise ValueError("numeric observation cannot use a boolean value")
    step = dict(payload.get("step") or {})
    if step and (not str(step.get("axis") or "").strip() or "value" not in step):
        raise ValueError("observation step requires axis and value")
    producer = dict(payload.get("producer") or {})
    producer.setdefault("attempt_id", session["attempt_id"])
    if producer["attempt_id"] != session["attempt_id"]:
        raise ValueError("observation producer attempt does not match tracking session")
    normalized = {
        "schema": "adaos.research.observation.v1",
        "metric": {"namespace": namespace, "name": name},
        "value": payload.get("value"),
        "value_type": value_type,
        "unit": str(payload.get("unit") or "1"),
        "direction": str(payload.get("direction") or "none"),
        "split_role": split_role,
        "dataset_digest": payload.get("dataset_digest"),
        "step": step,
        "aggregation": str(payload.get("aggregation") or "point"),
        # Provider-generated timestamps must be stable under an idempotent
        # retry. Producers that know the actual observation time supply it;
        # otherwise the attempt session opening is the deterministic fallback.
        "observed_at": str(payload.get("observed_at") or session["opened_at"]),
        "producer": producer,
        "evidence_role": str(payload.get("evidence_role") or "diagnostic"),
    }
    event_id = str(payload.get("event_id") or observation_event_id(str(session["session_id"]), normalized))
    return {**normalized, "event_id": event_id}


class LocalTracker:
    """Durable contract reference provider backed by the skill relational binding."""

    descriptor = TrackerDescriptor(
        provider_id="local-tracker",
        contract_version="1.0-rc1",
        capabilities=(
            "sessions",
            "typed-observations",
            "metric-history",
            "artifact-refs",
            "dataset-inputs",
            "idempotent-batch",
            "deterministic-export",
        ),
        limits={"max_batch_events": 500, "max_inline_value_bytes": 65536},
    )

    def __init__(
        self,
        repository: ResearchRepository,
        *,
        descriptor: TrackerDescriptor | None = None,
    ) -> None:
        self.repository = repository
        self.db = repository._db
        self.descriptor = descriptor or type(self).descriptor

    def health(self) -> dict[str, Any]:
        storage = dict(self.db.health())
        return {
            "ok": bool(storage.get("ok")),
            "state": "ready" if storage.get("ok") else "degraded",
            "descriptor": self.descriptor.to_dict(),
            "storage": storage,
        }

    @staticmethod
    def _decode_session(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **dict(row),
            "parameters": json.loads(str(row["parameters_json"])),
            "tags": json.loads(str(row["tags_json"])),
            "inputs": json.loads(str(row["inputs_json"])),
            "provider_binding": json.loads(str(row["provider_binding_json"])),
            "completeness": json.loads(str(row["completeness_json"])),
        }

    def get_session(self, session_id: str) -> dict[str, Any]:
        row = self.db.fetch_one(
            "SELECT * FROM tracker_sessions WHERE session_id=:session_id",
            {"session_id": session_id},
        )
        if not row:
            raise KeyError(session_id)
        return self._decode_session(row)

    def open_session(
        self,
        *,
        session_id: str,
        study_id: str,
        experiment_id: str,
        experiment_revision_id: str,
        trial_id: str,
        run_id: str,
        attempt_id: str,
        parameters: Mapping[str, Any],
        tags: Mapping[str, str],
        inputs: Sequence[Mapping[str, Any]] = (),
        provider_binding: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        candidate = {
            "session_id": session_id,
            "provider_id": self.descriptor.provider_id,
            "study_id": study_id,
            "experiment_id": experiment_id,
            "experiment_revision_id": experiment_revision_id,
            "trial_id": trial_id,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "status": "running",
            "parameters": dict(parameters),
            "tags": dict(tags),
            "inputs": [dict(item) for item in inputs],
            "provider_binding": dict(provider_binding or {}),
            "completeness": {},
        }
        existing = self.db.fetch_one(
            "SELECT * FROM tracker_sessions WHERE session_id=:session_id",
            {"session_id": session_id},
        )
        if existing:
            decoded = self._decode_session(existing)
            comparable = {key: decoded[key] for key in candidate}
            if canonical_json(comparable) != canonical_json(candidate):
                raise TrackerConflict("tracking session identity is bound to different content")
            return decoded
        opened_at = now()
        self.db.execute(
            "INSERT INTO tracker_sessions(session_id, provider_id, study_id, experiment_id, experiment_revision_id, trial_id, run_id, attempt_id, status, parameters_json, tags_json, inputs_json, provider_binding_json, completeness_json, opened_at, closed_at) VALUES (:session_id, :provider_id, :study_id, :experiment_id, :experiment_revision_id, :trial_id, :run_id, :attempt_id, :status, :parameters, :tags, :inputs, :provider_binding, :completeness, :opened_at, NULL)",
            {
                **{key: candidate[key] for key in ("session_id", "provider_id", "study_id", "experiment_id", "experiment_revision_id", "trial_id", "run_id", "attempt_id", "status")},
                "parameters": canonical_json(candidate["parameters"]),
                "tags": canonical_json(candidate["tags"]),
                "inputs": canonical_json(candidate["inputs"]),
                "provider_binding": canonical_json(candidate["provider_binding"]),
                "completeness": canonical_json({}),
                "opened_at": opened_at,
            },
        )
        return self.get_session(session_id)

    def _append_events(
        self,
        session_id: str,
        event_kind: str,
        payloads: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        session = self.get_session(session_id)
        if session["status"] != "running":
            raise ValueError("tracking session is finalized")
        if len(payloads) > int(self.descriptor.limits["max_batch_events"]):
            raise ValueError("tracker batch exceeds provider limit")
        accepted: list[str] = []
        duplicates: list[str] = []
        for source in payloads:
            payload = (
                normalize_observation(session, source)
                if event_kind == "observation"
                else dict(source)
            )
            if event_kind == "artifact":
                required = ("uri", "digest", "size_bytes", "media_type", "role")
                missing = [key for key in required if payload.get(key) in (None, "")]
                if missing:
                    raise ValueError(f"artifact reference is missing: {', '.join(missing)}")
                payload.setdefault("schema", "adaos.research.artifact_ref.v1")
                payload.setdefault("producer_attempt_id", session["attempt_id"])
                payload.setdefault(
                    "event_id",
                    identity(
                        "tracker_event",
                        {
                            "session_id": session_id,
                            "role": payload["role"],
                            "digest": payload["digest"],
                        },
                    ),
                )
                payload.setdefault("observed_at", session["opened_at"])
            event_id = str(payload["event_id"])
            payload_digest = digest(payload)
            existing = self.db.fetch_one(
                "SELECT payload_digest FROM tracker_events WHERE event_id=:event_id",
                {"event_id": event_id},
            )
            if existing:
                if str(existing["payload_digest"]) != payload_digest:
                    raise TrackerConflict(f"tracker event conflict: {event_id}")
                duplicates.append(event_id)
                continue
            ingested_at = now()
            local_delivery = self.descriptor.provider_id == "local-tracker"
            self.db.execute(
                "INSERT INTO tracker_events(event_id, session_id, event_kind, payload_json, payload_digest, observed_at, ingested_at, delivery_state, provider_receipt_json) VALUES (:event_id, :session_id, :event_kind, :payload, :payload_digest, :observed_at, :ingested_at, 'delivered', :receipt)",
                {
                    "event_id": event_id,
                    "session_id": session_id,
                    "event_kind": event_kind,
                    "payload": canonical_json(payload),
                    "payload_digest": payload_digest,
                    "observed_at": str(payload.get("observed_at") or ingested_at),
                    "ingested_at": ingested_at,
                    "receipt": canonical_json(
                        {"provider_id": self.descriptor.provider_id, "accepted": True}
                        if local_delivery
                        else {"provider_id": self.descriptor.provider_id, "accepted": False, "state": "pending"}
                    ),
                },
            )
            if not local_delivery:
                self.db.execute(
                    "UPDATE tracker_events SET delivery_state='pending' WHERE event_id=:event_id",
                    {"event_id": event_id},
                )
            accepted.append(event_id)
        return {
            "schema": "adaos.research.tracker_batch_receipt.v1",
            "session_id": session_id,
            "accepted": accepted,
            "duplicates": duplicates,
            "watermark": digest(sorted([*accepted, *duplicates])),
        }

    def pending_events(self, session_id: str, event_ids: Sequence[str] = ()) -> list[dict[str, Any]]:
        parameters: dict[str, Any] = {"session_id": session_id}
        statement = "SELECT event_id, event_kind, payload_json, payload_digest, delivery_state FROM tracker_events WHERE session_id=:session_id AND delivery_state!='delivered'"
        if event_ids:
            names = []
            for index, event_id in enumerate(event_ids):
                key = f"event_{index}"
                names.append(f":{key}")
                parameters[key] = event_id
            statement += f" AND event_id IN ({','.join(names)})"
        statement += " ORDER BY observed_at, event_id"
        return [
            {
                "event_id": str(row["event_id"]),
                "event_kind": str(row["event_kind"]),
                "payload": json.loads(str(row["payload_json"])),
                "payload_digest": str(row["payload_digest"]),
                "delivery_state": str(row["delivery_state"]),
            }
            for row in self.db.fetch_all(statement, parameters)
        ]

    def mark_delivery(
        self,
        event_ids: Sequence[str],
        *,
        state: str,
        receipt: Mapping[str, Any],
    ) -> None:
        if state not in {"pending", "delivered", "failed"}:
            raise ValueError("tracker delivery state is invalid")
        for event_id in event_ids:
            self.db.execute(
                "UPDATE tracker_events SET delivery_state=:state, provider_receipt_json=:receipt WHERE event_id=:event_id",
                {"state": state, "receipt": canonical_json(receipt), "event_id": event_id},
            )

    def append_observations(
        self, session_id: str, observations: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        return self._append_events(session_id, "observation", observations)

    def append_artifacts(
        self, session_id: str, artifacts: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        return self._append_events(session_id, "artifact", artifacts)

    def close_session(
        self,
        session_id: str,
        status: str,
        completeness: Mapping[str, Any],
    ) -> dict[str, Any]:
        if status not in FINAL_STATUSES:
            raise ValueError("tracker final status is invalid")
        session = self.get_session(session_id)
        if session["status"] != "running":
            if session["status"] != status or canonical_json(session["completeness"]) != canonical_json(completeness):
                raise TrackerConflict("tracking session is already finalized differently")
            return self.export_session(session_id)
        self.db.execute(
            "UPDATE tracker_sessions SET status=:status, completeness_json=:completeness, closed_at=:closed_at WHERE session_id=:session_id",
            {
                "status": status,
                "completeness": canonical_json(completeness),
                "closed_at": now(),
                "session_id": session_id,
            },
        )
        return self.export_session(session_id)

    def export_session(self, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        events = []
        for row in self.db.fetch_all(
            "SELECT event_id, event_kind, payload_json, payload_digest, delivery_state, provider_receipt_json FROM tracker_events WHERE session_id=:session_id ORDER BY event_kind, observed_at, event_id",
            {"session_id": session_id},
        ):
            events.append(
                {
                    "event_id": str(row["event_id"]),
                    "event_kind": str(row["event_kind"]),
                    "payload": json.loads(str(row["payload_json"])),
                    "payload_digest": str(row["payload_digest"]),
                    "delivery_state": str(row["delivery_state"]),
                    "provider_receipt": json.loads(str(row["provider_receipt_json"])),
                }
            )
        normalized_session = {
            key: session[key]
            for key in (
                "session_id",
                "provider_id",
                "study_id",
                "experiment_id",
                "experiment_revision_id",
                "trial_id",
                "run_id",
                "attempt_id",
                "status",
                "parameters",
                "tags",
                "inputs",
                "provider_binding",
                "completeness",
            )
        }
        value = {
            "schema": "adaos.research.tracker_export.v1",
            "contract_version": self.descriptor.contract_version,
            "session": normalized_session,
            "events": events,
        }
        return {**value, "export_digest": digest(value)}

    def export_experiment(self, experiment_id: str) -> dict[str, Any]:
        rows = self.db.fetch_all(
            "SELECT session_id FROM tracker_sessions WHERE experiment_id=:experiment_id ORDER BY opened_at, session_id",
            {"experiment_id": experiment_id},
        )
        sessions = [self.export_session(str(row["session_id"])) for row in rows]
        value = {
            "schema": "adaos.research.tracker_experiment_export.v1",
            "contract_version": self.descriptor.contract_version,
            "experiment_id": experiment_id,
            "sessions": sessions,
        }
        return {**value, "export_digest": digest(value)}

    def metric_history(self, session_id: str, namespace: str, name: str) -> list[dict[str, Any]]:
        exported = self.export_session(session_id)
        return [
            dict(item["payload"])
            for item in exported["events"]
            if item["event_kind"] == "observation"
            and item["payload"]["metric"] == {"namespace": namespace, "name": name}
        ]

    # Compatibility shim for the deterministic ARF1 fixture. New experiment
    # execution uses explicit attempt-bound sessions above.
    @staticmethod
    def _legacy_session_id(run_id: str) -> str:
        return identity("tracking_session", {"legacy_fixture_run_id": run_id})

    def register_run(
        self,
        *,
        run_id: str,
        study_id: str,
        trial_id: str,
        parameters: Mapping[str, Any],
        tags: Mapping[str, str],
    ) -> dict[str, Any]:
        return self.open_session(
            session_id=self._legacy_session_id(run_id),
            study_id=study_id,
            experiment_id=study_id,
            experiment_revision_id="legacy-fixture",
            trial_id=trial_id,
            run_id=run_id,
            attempt_id=f"legacy:{run_id}",
            parameters=parameters,
            tags=tags,
        )

    def observe(
        self,
        *,
        run_id: str,
        name: str,
        value: Any,
        split_role: str,
        step: int | None = None,
    ) -> dict[str, Any]:
        session_id = self._legacy_session_id(run_id)
        payload = {
            "metric": {"namespace": "fixture", "name": name},
            "value": value,
            "value_type": "float" if isinstance(value, float) else "integer",
            "split_role": split_role,
            "step": {"axis": "step", "value": step} if step is not None else {},
            "producer": {"component": "research.fixture", "sequence": step or 0},
        }
        normalized = normalize_observation(self.get_session(session_id), payload)
        self.append_observations(session_id, (normalized,))
        return {
            "observation_id": normalized["event_id"],
            "run_id": run_id,
            "name": name,
            "value": value,
            "step": step,
            "split_role": split_role,
            "recorded_at": normalized["observed_at"],
        }

    def finalize(self, run_id: str, status: str) -> dict[str, Any]:
        return self.close_session(
            self._legacy_session_id(run_id),
            status,
            {"legacy_fixture": True},
        )

    def export(self, run_id: str) -> dict[str, Any]:
        return self.export_session(self._legacy_session_id(run_id))


class MlflowTracker:
    """MLflow projection backed by the durable AdaOS tracker journal.

    AdaOS retains normalized, digestible evidence and an outbox. MLflow gets
    one native run per execution attempt for interactive telemetry queries.
    """

    descriptor = TrackerDescriptor(
        provider_id="mlflow",
        contract_version="1.0-rc1",
        capabilities=(
            "sessions",
            "typed-scalar-observations",
            "metric-history",
            "dataset-input-tags",
            "artifact-reference-tags",
            "idempotent-journal-projection",
            "native-query-ui",
        ),
        limits={
            "max_batch_events": 500,
            "metric_step": "integer",
            "parameter_value_bytes": 6000,
            "delivery_semantics": "transactional-outbox-at-least-once",
        },
    )

    def __init__(self, repository: ResearchRepository, endpoint: str) -> None:
        self.endpoint = str(endpoint).rstrip("/")
        if not self.endpoint.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValueError("the initial MLflow provider only admits a loopback tracking endpoint")
        self.journal = LocalTracker(repository, descriptor=self.descriptor)

    def _request(
        self,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        method: str | None = None,
    ) -> dict[str, Any]:
        body = None if payload is None else canonical_json(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.endpoint}{path}",
            data=body,
            method=method or ("POST" if body is not None else "GET"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5.0) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", errors="replace")
            raise RuntimeError(f"MLflow request failed ({exc.code}): {detail[:500]}") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise RuntimeError(f"MLflow provider is unavailable: {type(exc).__name__}") from exc
        if not raw.strip():
            return {}
        try:
            return dict(json.loads(raw))
        except json.JSONDecodeError:
            return {"body": raw}

    def health(self) -> dict[str, Any]:
        try:
            upstream = self._request("/health")
        except RuntimeError as exc:
            return {
                "ok": False,
                "state": "unavailable",
                "descriptor": self.descriptor.to_dict(),
                "endpoint": self.endpoint,
                "error": str(exc),
            }
        return {
            "ok": True,
            "state": "ready",
            "descriptor": self.descriptor.to_dict(),
            "endpoint": self.endpoint,
            "ui_url": self.endpoint,
            "upstream": upstream,
            "journal": self.journal.health(),
        }

    @staticmethod
    def _tags(values: Mapping[str, Any]) -> list[dict[str, str]]:
        return [{"key": str(key), "value": str(value)} for key, value in sorted(values.items())]

    def _experiment_id(self, session: Mapping[str, Any]) -> str:
        name = f"adaos/{session['study_id']}/{session['experiment_id']}"
        query = urllib.parse.urlencode({"experiment_name": name})
        try:
            response = self._request(f"/api/2.0/mlflow/experiments/get-by-name?{query}")
            return str(dict(response["experiment"])["experiment_id"])
        except (KeyError, RuntimeError):
            try:
                created = self._request(
                    "/api/2.0/mlflow/experiments/create",
                    {
                        "name": name,
                        "tags": self._tags(
                            {
                                "adaos.study_id": session["study_id"],
                                "adaos.experiment_id": session["experiment_id"],
                                "adaos.contract_version": self.descriptor.contract_version,
                            }
                        ),
                    },
                )
                return str(created["experiment_id"])
            except RuntimeError:
                response = self._request(f"/api/2.0/mlflow/experiments/get-by-name?{query}")
                return str(dict(response["experiment"])["experiment_id"])

    def _ensure_run(self, session: Mapping[str, Any]) -> dict[str, str]:
        experiment_id = self._experiment_id(session)
        session_id = str(session["session_id"])
        searched = self._request(
            "/api/2.0/mlflow/runs/search",
            {
                "experiment_ids": [experiment_id],
                "filter": f"tags.`adaos.session_id` = '{session_id}'",
                "max_results": 1,
            },
        )
        runs = list(searched.get("runs") or [])
        if runs:
            run_id = str(dict(runs[0]["info"])["run_id"])
        else:
            tags = {
                **dict(session["tags"]),
                "adaos.session_id": session_id,
                "adaos.study_id": session["study_id"],
                "adaos.experiment_id": session["experiment_id"],
                "adaos.experiment_revision_id": session["experiment_revision_id"],
                "adaos.trial_id": session["trial_id"],
                "adaos.run_id": session["run_id"],
                "adaos.attempt_id": session["attempt_id"],
                "adaos.contract_version": self.descriptor.contract_version,
            }
            for index, item in enumerate(session.get("inputs") or []):
                tags[f"adaos.input.{index}"] = canonical_json(item)
            created = self._request(
                "/api/2.0/mlflow/runs/create",
                {
                    "experiment_id": experiment_id,
                    "start_time": int(time.time() * 1000),
                    "tags": self._tags(tags),
                },
            )
            run_id = str(dict(created["run"])["info"]["run_id"])
        current = self._request(
            f"/api/2.0/mlflow/runs/get?{urllib.parse.urlencode({'run_id': run_id})}"
        )
        existing_params = dict(dict(current.get("run") or {}).get("data") or {}).get("params") or []
        existing_keys = {str(item["key"]) for item in existing_params}
        params = [
            {"key": str(key), "value": value if isinstance(value, str) else canonical_json(value)}
            for key, value in sorted(dict(session["parameters"]).items())
            if str(key) not in existing_keys
        ]
        if params:
            self._request(
                "/api/2.0/mlflow/runs/log-batch",
                {"run_id": run_id, "metrics": [], "params": params, "tags": []},
            )
        return {"experiment_id": experiment_id, "run_id": run_id}

    @staticmethod
    def _timestamp(value: Any) -> int:
        try:
            return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return int(time.time() * 1000)

    def _project(self, session_id: str, event_ids: Sequence[str]) -> dict[str, Any]:
        pending = self.journal.pending_events(session_id, event_ids)
        if not pending:
            return {"state": "delivered", "delivered": []}
        ids = [item["event_id"] for item in pending]
        try:
            session = self.journal.get_session(session_id)
            binding = self._ensure_run(session)
            metrics: list[dict[str, Any]] = []
            tags: list[dict[str, str]] = []
            for event in pending:
                payload = dict(event["payload"])
                if event["event_kind"] == "observation" and isinstance(payload.get("value"), (int, float)) and not isinstance(payload.get("value"), bool):
                    metric = dict(payload["metric"])
                    step = dict(payload.get("step") or {})
                    metrics.append(
                        {
                            "key": f"{payload['split_role']}.{metric['namespace']}.{metric['name']}",
                            "value": float(payload["value"]),
                            "timestamp": self._timestamp(payload.get("observed_at")),
                            "step": int(step.get("value") or 0),
                        }
                    )
                else:
                    suffix = str(event["event_id"]).split(".")[-1][:16]
                    tags.append(
                        {
                            "key": f"adaos.event.{suffix}",
                            "value": canonical_json(
                                {
                                    "kind": event["event_kind"],
                                    "digest": event["payload_digest"],
                                    "payload": payload,
                                }
                            )[:5000],
                        }
                    )
            self._request(
                "/api/2.0/mlflow/runs/log-batch",
                {"run_id": binding["run_id"], "metrics": metrics, "params": [], "tags": tags},
            )
            receipt = {
                "provider_id": "mlflow",
                "state": "delivered",
                "mlflow_experiment_id": binding["experiment_id"],
                "mlflow_run_id": binding["run_id"],
            }
            self.journal.mark_delivery(ids, state="delivered", receipt=receipt)
            return {**receipt, "delivered": ids}
        except Exception as exc:
            receipt = {
                "provider_id": "mlflow",
                "state": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
            self.journal.mark_delivery(ids, state="failed", receipt=receipt)
            return {**receipt, "delivered": []}

    def open_session(self, **values: Any) -> dict[str, Any]:
        provider_binding = {
            "provider_id": "mlflow",
            "tracking_uri": self.endpoint,
            "ui_url": self.endpoint,
            "authority": "telemetry-projection",
        }
        session = self.journal.open_session(**{**values, "provider_binding": provider_binding})
        try:
            binding = self._ensure_run(session)
        except RuntimeError:
            binding = {"state": "pending"}
        return {**session, "projection": binding}

    def append_observations(
        self, session_id: str, observations: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        receipt = self.journal.append_observations(session_id, observations)
        projection = self._project(session_id, [*receipt["accepted"], *receipt["duplicates"]])
        return {**receipt, "projection": projection}

    def append_artifacts(
        self, session_id: str, artifacts: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        receipt = self.journal.append_artifacts(session_id, artifacts)
        projection = self._project(session_id, [*receipt["accepted"], *receipt["duplicates"]])
        return {**receipt, "projection": projection}

    def close_session(
        self, session_id: str, status: str, completeness: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._project(session_id, [item["event_id"] for item in self.journal.pending_events(session_id)])
        exported = self.journal.close_session(session_id, status, completeness)
        try:
            binding = self._ensure_run(exported["session"])
            mapped = {"succeeded": "FINISHED", "failed": "FAILED", "cancelled": "KILLED", "lost": "FAILED"}[status]
            self._request(
                "/api/2.0/mlflow/runs/update",
                {"run_id": binding["run_id"], "status": mapped, "end_time": int(time.time() * 1000)},
            )
        except RuntimeError:
            pass
        return self.journal.export_session(session_id)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self.journal.get_session(session_id)

    def export_session(self, session_id: str) -> dict[str, Any]:
        return self.journal.export_session(session_id)

    def export_experiment(self, experiment_id: str) -> dict[str, Any]:
        return self.journal.export_experiment(experiment_id)

    def metric_history(self, session_id: str, namespace: str, name: str) -> list[dict[str, Any]]:
        return self.journal.metric_history(session_id, namespace, name)


__all__ = [
    "FINAL_STATUSES",
    "LocalTracker",
    "MlflowTracker",
    "TrackerConflict",
    "TrackerDescriptor",
    "TrackerProvider",
    "normalize_observation",
    "observation_event_id",
]
