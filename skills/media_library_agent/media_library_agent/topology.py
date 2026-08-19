from __future__ import annotations

from typing import Any, Mapping

from adaos.sdk import distributed as distributed_sdk


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
