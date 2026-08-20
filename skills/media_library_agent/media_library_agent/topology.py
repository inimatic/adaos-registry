from __future__ import annotations

import base64
import hashlib
import zlib
from dataclasses import replace
from typing import Any, Mapping

from adaos.sdk import distributed as distributed_sdk

from .contracts import json_dumps, json_loads, now_iso, stable_id, text
from .repository import MediaLibraryAgentRepository


MAX_INLINE_SNAPSHOT_BYTES = 64 * 1024
MAX_INLINE_SNAPSHOT_EXPANDED_BYTES = 512 * 1024


class LibraryAgentTopology:
    """Validate node-local shard evidence for the authority-plane SDK."""

    def observe(
        self,
        repository: MediaLibraryAgentRepository,
        partition: Mapping[str, Any],
        replica: Mapping[str, Any],
    ) -> dict[str, Any]:
        partition_value = distributed_sdk.Partition.from_mapping(partition)
        replica_value = distributed_sdk.Replica.from_mapping(replica)
        if (
            replica_value.partition_id != partition_value.partition_id
            or replica_value.node_id != repository.node_id
        ):
            raise ValueError("topology_observation_identity_mismatch")
        root_id = text(partition_value.selector.get("root_id"))
        if root_id:
            witness = repository.topology_root_witness(root_id)
            if witness is None:
                raise ValueError("external_root_not_present_on_agent")
            replica_value = replace(
                replica_value,
                checkpoint=text(witness.get("checkpoint")) or None,
                source_ref=f"media-root:{root_id}",
                content_state=(
                    "non_empty" if int(witness.get("available") or 0) else "empty"
                ),
                item_count=int(witness.get("available") or 0),
                byte_count=int(witness.get("bytes") or 0),
                freshness_seconds=0,
                observed_at=now_iso(),
            )
        return {
            "ok": True,
            "partition": partition_value.to_dict(),
            "replica": replica_value.to_dict(),
            "external_media_copied": False,
        }

    def execute_phase(
        self,
        repository: MediaLibraryAgentRepository,
        payload: Mapping[str, Any],
        *,
        resource_pressure: str,
    ) -> dict[str, Any]:
        request = dict(payload)
        request_digest = "sha256:" + hashlib.sha256(
            json_dumps(request).encode("utf-8")
        ).hexdigest()
        idempotency_key = text(request.get("idempotency_key"))
        previous = repository.topology_phase_receipt(idempotency_key)
        if previous is not None:
            if previous["request_digest"] != request_digest:
                return {
                    "ok": False,
                    "error_code": "topology_phase_idempotency_conflict",
                }
            return dict(previous["result"])
        result = self._execute_phase(
            repository,
            request,
            resource_pressure=resource_pressure,
        )
        return repository.save_topology_phase_receipt(
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            operation_id=text(request.get("operation_id")),
            phase=text(request.get("phase")),
            result=result,
        )

    def _execute_phase(
        self,
        repository: MediaLibraryAgentRepository,
        payload: Mapping[str, Any],
        *,
        resource_pressure: str,
    ) -> dict[str, Any]:
        if payload.get("schema") != "adaos.distributed.topology_phase_request.v1":
            return {"ok": False, "error_code": "topology_phase_schema_invalid"}
        phase = text(payload.get("phase"))
        partition = dict(payload.get("partition") or {})
        dataset = dict(payload.get("dataset") or {})
        selected = self._selected_instance(payload)
        if selected is None or text(selected.get("node_id")) != repository.node_id:
            return {"ok": False, "error_code": "topology_phase_node_mismatch"}
        root_id = text((partition.get("selector") or {}).get("root_id"))
        partition_id = text(partition.get("partition_id"))
        witness = repository.topology_root_witness(root_id) if root_id else None
        if dataset.get("consistency_profile") == "external_authority" and witness is None:
            return {
                "ok": False,
                "error_code": "external_root_not_present_on_target",
            }
        if phase == "reserve" and resource_pressure in {"playback", "critical"}:
            return {
                "ok": False,
                "retryable": True,
                "error_code": "media_agent_resource_pressure",
            }
        previous = None if witness is not None else self._selected_replica(payload)
        if phase == "catch_up":
            imported = self._import_inline_snapshot(
                repository,
                payload,
                partition_id=partition_id,
            )
            if imported is not None:
                return imported
        local_snapshot = (
            repository.topology_replica_snapshot(partition_id)
            if witness is None and previous is None
            else None
        )
        evidence = dict(witness or previous or local_snapshot or {})
        if phase in {"catch_up", "verify", "activate_read", "promote"}:
            mismatch = self._target_witness_error(payload, evidence=evidence)
            if mismatch is not None:
                return {"ok": False, "error_code": mismatch}
        checkpoint = text(evidence.get("checkpoint")) or None
        receipt = {
            "phase": phase,
            "partition_id": text(partition.get("partition_id")),
            "instance_id": text(selected.get("instance_id")),
            "node_id": repository.node_id,
            "checkpoint": checkpoint,
            "content_witness": evidence.get("content_witness") or checkpoint,
            "item_count": int(evidence.get("available") or evidence.get("item_count") or 0),
            "byte_count": int(evidence.get("bytes") or evidence.get("byte_count") or 0),
            "external_media_copied": False,
        }
        if phase in {"snapshot", "stream_deltas"}:
            source = payload.get("source_instance")
            if isinstance(source, Mapping) and text(source.get("instance_id")) == text(
                selected.get("instance_id")
            ):
                snapshot = self._export_inline_snapshot(
                    repository,
                    root_id=root_id,
                    evidence=evidence,
                )
                if snapshot.get("ok") is not True:
                    return snapshot
                receipt["inline_snapshot"] = snapshot["inline_snapshot"]
        if phase in {"activate_read", "promote", "demote", "drain", "remove"}:
            observed = self._observe_phase_replica(
                repository,
                payload,
                selected=selected,
                witness=evidence,
            )
            if not observed.get("ok"):
                return observed
            receipt["replica"] = observed["replica"]
        if phase == "release":
            receipt["released"] = True
        return {"ok": True, "receipt": receipt}

    @staticmethod
    def _export_inline_snapshot(
        repository: MediaLibraryAgentRepository,
        *,
        root_id: str,
        evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        snapshot = repository.topology_catalog_snapshot(
            root_id=root_id,
            max_items=1000,
        )
        if snapshot.get("has_more") is True:
            return {
                "ok": False,
                "error_code": "media_agent_topology_snapshot_data_plane_required",
            }
        raw = json_dumps(snapshot).encode("utf-8")
        compressed = zlib.compress(raw, level=6)
        if (
            len(raw) > MAX_INLINE_SNAPSHOT_EXPANDED_BYTES
            or len(compressed) > MAX_INLINE_SNAPSHOT_BYTES
        ):
            return {
                "ok": False,
                "error_code": "media_agent_topology_snapshot_data_plane_required",
            }
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        checkpoint = text(evidence.get("checkpoint") or snapshot.get("checkpoint"))
        return {
            "ok": True,
            "inline_snapshot": {
                "schema": "adaos.distributed.inline_snapshot.v1",
                "encoding": "zlib+base64",
                "payload": base64.b64encode(compressed).decode("ascii"),
                "payload_digest": digest,
                "checkpoint": checkpoint,
                "content_witness": text(
                    evidence.get("content_witness")
                    or snapshot.get("content_witness")
                    or checkpoint
                ),
                "item_count": int(
                    evidence.get("available")
                    or evidence.get("item_count")
                    or snapshot.get("item_count")
                    or 0
                ),
                "byte_count": int(
                    evidence.get("bytes")
                    or evidence.get("byte_count")
                    or snapshot.get("byte_count")
                    or 0
                ),
            },
        }

    @staticmethod
    def _import_inline_snapshot(
        repository: MediaLibraryAgentRepository,
        payload: Mapping[str, Any],
        *,
        partition_id: str,
    ) -> dict[str, Any] | None:
        phase_inputs = payload.get("phase_inputs")
        snapshot = (
            phase_inputs.get("source_snapshot")
            if isinstance(phase_inputs, Mapping)
            else None
        )
        if not isinstance(snapshot, Mapping):
            return {
                "ok": False,
                "error_code": "topology_snapshot_input_missing",
            }
        if (
            snapshot.get("schema") != "adaos.distributed.inline_snapshot.v1"
            or snapshot.get("encoding") != "zlib+base64"
        ):
            return {
                "ok": False,
                "error_code": "topology_snapshot_input_invalid",
            }
        try:
            compressed = base64.b64decode(text(snapshot.get("payload")), validate=True)
            if len(compressed) > MAX_INLINE_SNAPSHOT_BYTES:
                raise ValueError("compressed snapshot exceeds inline limit")
            decompressor = zlib.decompressobj()
            raw = decompressor.decompress(
                compressed,
                MAX_INLINE_SNAPSHOT_EXPANDED_BYTES + 1,
            )
            raw += decompressor.flush()
            if len(raw) > MAX_INLINE_SNAPSHOT_EXPANDED_BYTES or not decompressor.eof:
                raise ValueError("expanded snapshot exceeds inline limit")
            digest = "sha256:" + hashlib.sha256(raw).hexdigest()
            if digest != text(snapshot.get("payload_digest")):
                raise ValueError("snapshot digest mismatch")
            value = json_loads(raw.decode("utf-8"), None)
            if not isinstance(value, Mapping):
                raise ValueError("snapshot payload must be an object")
            if value.get("schema") != "adaos.media_library.catalog_snapshot.v1":
                raise ValueError("snapshot payload schema mismatch")
        except (ValueError, zlib.error, UnicodeDecodeError):
            return {
                "ok": False,
                "error_code": "topology_snapshot_input_invalid",
            }
        repository.save_topology_replica_snapshot(
            partition_id,
            checkpoint=text(snapshot.get("checkpoint")),
            content_witness=text(snapshot.get("content_witness")),
            payload_digest=digest,
            item_count=int(snapshot.get("item_count") or 0),
            byte_count=int(snapshot.get("byte_count") or 0),
            payload=value,
        )
        return None

    @staticmethod
    def _selected_instance(payload: Mapping[str, Any]) -> dict[str, Any] | None:
        selected_id = text(payload.get("selected_instance_id"))
        for value in (payload.get("source_instance"), payload.get("target_instance")):
            if isinstance(value, Mapping) and text(value.get("instance_id")) == selected_id:
                return dict(value)
        return None

    def _observe_phase_replica(
        self,
        repository: MediaLibraryAgentRepository,
        payload: Mapping[str, Any],
        *,
        selected: Mapping[str, Any],
        witness: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        phase = text(payload.get("phase"))
        partition = dict(payload.get("partition") or {})
        step = dict(payload.get("step") or {})
        partition_id = text(partition.get("partition_id"))
        instance_id = text(selected.get("instance_id"))
        replica_id = stable_id("replica", partition_id, instance_id, size=28)
        previous = self._selected_replica(payload)
        if previous is not None and text(previous.get("replica_id")) != replica_id:
            return {"ok": False, "error_code": "topology_replica_identity_mismatch"}
        evidence = dict(witness or previous or {})
        role = text(step.get("replica_role")) or "derived"
        lifecycle = "ready"
        if phase == "promote":
            role = "authority"
        elif phase == "demote":
            role = "follower"
        elif phase == "drain":
            role = text((previous or {}).get("role")) or role
            lifecycle = "draining"
        elif phase == "remove":
            role = text((previous or {}).get("role")) or role
            lifecycle = "removed"
        authority_epoch = int(payload.get("authority_epoch") or 0)
        if role == "authority" and authority_epoch < 1:
            return {"ok": False, "error_code": "authority_epoch_missing"}
        replica = distributed_sdk.Replica(
            replica_id=replica_id,
            partition_id=partition_id,
            instance_id=instance_id,
            node_id=repository.node_id,
            role=role,
            lifecycle=lifecycle,
            content_state=(
                text(evidence.get("content_state"))
                or (
                    "non_empty"
                    if int(evidence.get("available") or evidence.get("item_count") or 0)
                    else "empty"
                )
            ),
            authority_epoch=authority_epoch,
            checkpoint=text(evidence.get("checkpoint")) or None,
            source_ref=(
                f"media-root:{text((partition.get('selector') or {}).get('root_id'))}"
                if text((partition.get("selector") or {}).get("root_id"))
                else text(evidence.get("source_ref")) or None
            ),
            freshness_seconds=0,
            item_count=int(evidence.get("available") or evidence.get("item_count") or 0),
            byte_count=int(evidence.get("bytes") or evidence.get("byte_count") or 0),
            observed_at=now_iso(),
            revision=int((previous or {}).get("revision") or 0) + 1,
        )
        return {"ok": True, "replica": replica.to_dict()}

    @staticmethod
    def _selected_replica(payload: Mapping[str, Any]) -> dict[str, Any] | None:
        selected_id = text(payload.get("selected_instance_id"))
        for value in (payload.get("source_replica"), payload.get("target_replica")):
            if isinstance(value, Mapping) and text(value.get("instance_id")) == selected_id:
                return dict(value)
        return None

    @staticmethod
    def _target_witness_error(
        payload: Mapping[str, Any], *, evidence: Mapping[str, Any]
    ) -> str | None:
        source = payload.get("source_replica")
        target = payload.get("target_instance")
        selected_id = text(payload.get("selected_instance_id"))
        if not isinstance(source, Mapping) or not isinstance(target, Mapping):
            return None
        if selected_id != text(target.get("instance_id")):
            return None

        expected_checkpoint = text(source.get("checkpoint"))
        observed_checkpoint = text(
            evidence.get("checkpoint") or evidence.get("content_witness")
        )
        expected_items = int(source.get("item_count") or 0)
        observed_items = int(evidence.get("available") or evidence.get("item_count") or 0)
        if expected_checkpoint and observed_checkpoint != expected_checkpoint:
            return "topology_target_content_witness_mismatch"
        if expected_items > observed_items:
            return "topology_target_content_incomplete"
        if text(source.get("content_state")).lower() == "non_empty" and observed_items <= 0:
            return "topology_target_content_incomplete"
        return None
