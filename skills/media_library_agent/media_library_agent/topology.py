from __future__ import annotations

import hashlib
from typing import Any, Mapping

from adaos.sdk import distributed as distributed_sdk

from .contracts import json_dumps, now_iso, stable_id, text
from .repository import MediaLibraryAgentRepository


class LibraryAgentTopology:
    """Node-agent membership and shard observations through the public SDK."""

    def join(self, instance: Mapping[str, Any], *, expected_revision: int = 0, lease_seconds: int = 90) -> dict[str, Any]:
        registered = distributed_sdk.register(
            distributed_sdk.ServiceInstance.from_mapping(instance),
            expected_revision=max(0, int(expected_revision)),
            lease_seconds=max(30, min(int(lease_seconds), 300)),
        )
        return {"ok": True, "instance": registered.to_dict()}

    def renew(
        self,
        instance_id: str,
        *,
        expected_revision: int,
        readiness: bool,
        status: str,
        health: Mapping[str, Any],
        pressure: Mapping[str, Any],
        lease_seconds: int = 90,
    ) -> dict[str, Any]:
        observed = distributed_sdk.renew(
            instance_id,
            expected_revision=max(1, int(expected_revision)),
            readiness=bool(readiness),
            status=status,
            health=health,
            pressure=pressure,
            lease_seconds=max(30, min(int(lease_seconds), 300)),
        )
        return {"ok": True, "instance": observed.to_dict()}

    def observe(self, partition: Mapping[str, Any], replica: Mapping[str, Any]) -> dict[str, Any]:
        partition_value = distributed_sdk.Partition.from_mapping(partition)
        replica_value = distributed_sdk.Replica.from_mapping(replica)
        saved_partition = distributed_sdk.put_partition(
            partition_value,
            expected_revision=max(0, partition_value.revision - 1),
        )
        saved_replica = distributed_sdk.observe_replica(
            replica_value,
            expected_revision=max(0, replica_value.revision - 1),
        )
        return {"ok": True, "partition": saved_partition.to_dict(), "replica": saved_replica.to_dict()}

    def drain(self, instance_id: str, *, expected_revision: int) -> dict[str, Any]:
        instance = distributed_sdk.drain(instance_id, expected_revision=max(1, int(expected_revision)))
        return {"ok": True, "instance": instance.to_dict()}

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
        previous = (
            None
            if witness is not None
            else self._replica_for_partition_instance(
                text(partition.get("partition_id")),
                text(selected.get("instance_id")),
            )
        )
        evidence = dict(witness or previous or {})
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
        if phase in {"activate_read", "promote", "demote", "drain", "remove"}:
            observed = self._observe_phase_replica(
                repository,
                payload,
                selected=selected,
                witness=witness,
            )
            if not observed.get("ok"):
                return observed
            receipt["replica"] = observed["replica"]
        if phase == "release":
            receipt["released"] = True
        return {"ok": True, "receipt": receipt}

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
        previous = self._find_replica(replica_id)
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
        saved = distributed_sdk.observe_replica(
            replica,
            expected_revision=int((previous or {}).get("revision") or 0),
        )
        return {"ok": True, "replica": saved.to_dict()}

    @staticmethod
    def _replica_for_partition_instance(
        partition_id: str,
        instance_id: str,
    ) -> dict[str, Any] | None:
        if not partition_id or not instance_id:
            return None
        return LibraryAgentTopology._find_replica(
            stable_id("replica", partition_id, instance_id, size=28)
        )

    @staticmethod
    def _find_replica(replica_id: str) -> dict[str, Any] | None:
        cursors: dict[str, str | None] = {}
        for _ in range(20):
            inspection = distributed_sdk.inspect(cursors=cursors, limit=100)
            for replica in inspection.replicas:
                if replica.replica_id == replica_id:
                    return replica.to_dict()
            cursor = inspection.cursors.get("replicas")
            if not cursor:
                return None
            cursors = {"replicas": cursor}
        raise RuntimeError("replica_inventory_limit_exceeded")
