from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from adaos.sdk import deployment as deployment_sdk
from adaos.sdk import distributed as distributed_sdk


DEPLOYMENT_ID = "media-center-home"
MEDIA_DATASET_IDS = frozenset(
    {"media-catalog", "media-catalog-authority", "media-files"}
)


def _dict(value: Any) -> dict[str, Any]:
    method = getattr(value, "to_dict", None)
    return dict(method()) if callable(method) else dict(value or {})


class MediaCenterTopology:
    """Media policy projected over public deployment and distributed SDKs."""

    def agent_instances(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return ready library agents with a current membership lease."""

        bounded = max(1, min(int(limit or 100), 1000))
        now = datetime.now(timezone.utc)
        lease_cursor: str | None = None
        active_leases: dict[str, Any] = {}
        while True:
            inspection = distributed_sdk.inspect(
                cursors={"leases": lease_cursor} if lease_cursor else None,
                limit=100,
            )
            for lease in inspection.leases:
                if lease.kind != "membership" or lease.status != "active":
                    continue
                try:
                    valid_until = datetime.fromisoformat(
                        lease.valid_until.replace("Z", "+00:00")
                    )
                except ValueError:
                    continue
                if valid_until <= now:
                    continue
                active_leases[lease.owner_instance_id] = lease
            lease_cursor = inspection.cursors.get("leases")
            if not lease_cursor:
                break

        cursor: str | None = None
        instances: list[dict[str, Any]] = []
        while len(instances) < bounded:
            inspection = distributed_sdk.inspect(
                cursors={"instances": cursor} if cursor else None,
                limit=min(100, bounded - len(instances)),
            )
            for instance in inspection.instances:
                if instance.component_ref != "skill:media_library_agent":
                    continue
                if instance.status != "ready" or not instance.readiness:
                    continue
                lease = active_leases.get(instance.instance_id)
                if (
                    lease is None
                    or lease.lease_id != instance.lease_id
                    or lease.topology_generation != instance.topology_generation
                ):
                    continue
                instances.append(instance.to_dict())
                if len(instances) >= bounded:
                    break
            cursor = inspection.cursors.get("instances")
            if not cursor:
                break
        return instances

    def invoke_agent(
        self,
        instance_id: str,
        operation: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float = 30.0,
        request_id: str = "",
    ) -> dict[str, Any]:
        result = distributed_sdk.invoke(
            instance_id,
            operation,
            arguments or {},
            request_id=request_id or f"media-center-agent-{uuid4().hex}",
            timeout_seconds=max(1.0, min(float(timeout_seconds), 600.0)),
        )
        if not isinstance(result, Mapping):
            raise RuntimeError("media_library_agent_invalid_response")
        return dict(result)

    def deployment_status(self, deployment_id: str = DEPLOYMENT_ID, *, limit: int = 50) -> dict[str, Any]:
        page_limit = max(1, min(int(limit), 100))
        try:
            inspection = deployment_sdk.inspect(deployment_id, limit=page_limit)
        except Exception as exc:
            return {
                "ok": False,
                "schema": "adaos.media_center.deployment_admin.v1",
                "deployment_id": deployment_id,
                "state": "unavailable",
                "reason": str(exc)[:300],
                "nodes": [],
                "operations": [],
            }
        value = inspection.to_dict()
        activations = list(value.get("activations") or [])
        operations = list(value.get("operations") or [])
        activation_cursor = value.get("activation_cursor")
        operation_cursor = value.get("operation_cursor")
        page_budget = 20
        activation_pages = 1
        history_error = ""
        try:
            while activation_cursor and activation_pages < page_budget:
                page = deployment_sdk.inspect(
                    deployment_id,
                    activation_cursor=str(activation_cursor),
                    limit=page_limit,
                ).to_dict()
                activations.extend(page.get("activations") or [])
                activation_cursor = page.get("activation_cursor")
                activation_pages += 1
        except Exception as exc:
            history_error = str(exc)[:300]
        operation_pages = 1
        try:
            while operation_cursor and operation_pages < page_budget:
                page = deployment_sdk.inspect(
                    deployment_id,
                    operation_cursor=str(operation_cursor),
                    limit=page_limit,
                ).to_dict()
                operations.extend(page.get("operations") or [])
                operation_cursor = page.get("operation_cursor")
                operation_pages += 1
        except Exception as exc:
            history_error = history_error or str(exc)[:300]
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for activation in activations:
            node_id = str(activation.get("node_id") or "")
            component_ref = str(activation.get("component_ref") or "")
            if not node_id or not component_ref:
                continue
            key = (node_id, component_ref)
            current = latest.get(key)
            candidate_rank = (
                int(activation.get("generation") or 0),
                str(activation.get("updated_at") or ""),
                str(activation.get("activation_id") or ""),
            )
            current_rank = (
                int(current.get("generation") or 0),
                str(current.get("updated_at") or ""),
                str(current.get("activation_id") or ""),
            ) if current is not None else (-1, "", "")
            if candidate_rank > current_rank:
                latest[key] = activation
        nodes: dict[str, dict[str, Any]] = {}
        for activation in latest.values():
            if activation.get("status") == "removed":
                continue
            node_id = str(activation.get("node_id") or "")
            row = nodes.setdefault(
                node_id,
                {
                    "node_id": node_id,
                    "state": "active",
                    "generation": 0,
                    "components": [],
                    "agent": False,
                },
            )
            row["components"].append(str(activation.get("component_ref") or ""))
            row["generation"] = max(int(row["generation"]), int(activation.get("generation") or 0))
            row["agent"] = row["agent"] or activation.get("component_ref") == "skill:media_library_agent"
            if activation.get("status") != "active":
                row["state"] = activation.get("status")
        for row in nodes.values():
            row["components"].sort()
        operations.sort(
            key=lambda item: (
                str(item.get("updated_at") or ""),
                str(item.get("created_at") or ""),
                str(item.get("operation_id") or ""),
            ),
            reverse=True,
        )
        desired = dict(value.get("desired") or {})
        return {
            "ok": True,
            "schema": "adaos.media_center.deployment_admin.v1",
            "deployment_id": deployment_id,
            "state": desired.get("status") or "unknown",
            "desired_revision": desired.get("revision"),
            "release_digest": desired.get("release_digest"),
            "placements": desired.get("placements") or [],
            "nodes": list(nodes.values())[:100],
            "operations": operations[:page_limit],
            "next_activation_cursor": activation_cursor,
            "next_operation_cursor": operation_cursor,
            "history_truncated": bool(activation_cursor or operation_cursor),
            "history_error": history_error or None,
        }

    def configure_deployment(
        self,
        *,
        release_digest: str,
        subnet_id: str,
        coordinator_node_id: str = "",
        agent_node_ids: list[str] | tuple[str, ...] = (),
        all_matching_agents: bool = False,
        expected_revision: int = 0,
        allow_release_skew: bool = False,
        reason: str = "Media Center placement update",
        deployment_id: str = DEPLOYMENT_ID,
    ) -> dict[str, Any]:
        coordinator = (
            deployment_sdk.ComponentPlacementPolicy(
                component_ref="skill:media_center_skill",
                mode="selected_nodes",
                selected_node_ids=(coordinator_node_id,),
                required_capabilities=("project.activate",),
                required_capacity={"cpu_millicores": 250, "memory_mb": 128},
            )
            if coordinator_node_id
            else deployment_sdk.ComponentPlacementPolicy(
                component_ref="skill:media_center_skill",
                mode="singleton",
                required_capabilities=("project.activate",),
                required_capacity={"cpu_millicores": 250, "memory_mb": 128},
            )
        )
        agents = (
            deployment_sdk.ComponentPlacementPolicy(
                component_ref="skill:media_library_agent",
                mode="all_matching",
                min_instances=1,
                max_instances=32,
                required_capabilities=("project.activate", "media.catalog"),
                required_capacity={"cpu_millicores": 500, "memory_mb": 128},
            )
            if all_matching_agents
            else deployment_sdk.ComponentPlacementPolicy(
                component_ref="skill:media_library_agent",
                mode="selected_nodes",
                selected_node_ids=tuple(agent_node_ids or ([coordinator_node_id] if coordinator_node_id else [])),
                required_capabilities=("project.activate", "media.catalog"),
                required_capacity={"cpu_millicores": 500, "memory_mb": 128},
            )
            if agent_node_ids or coordinator_node_id
            else deployment_sdk.ComponentPlacementPolicy(
                component_ref="skill:media_library_agent",
                mode="co_located_with",
                co_located_with="skill:media_center_skill",
                required_capacity={"cpu_millicores": 500, "memory_mb": 128},
            )
        )
        desired = deployment_sdk.ProjectDeployment(
            deployment_id=deployment_id,
            project_ref="project:media_center",
            release_digest=release_digest,
            subnet_id=subnet_id,
            revision=max(1, int(expected_revision) + 1),
            placements=(
                coordinator,
                agents,
                deployment_sdk.ComponentPlacementPolicy(
                    component_ref="skill:media_control_skill",
                    mode="co_located_with",
                    co_located_with="skill:media_center_skill",
                ),
                deployment_sdk.ComponentPlacementPolicy(
                    component_ref="scenario:media_center",
                    mode="co_located_with",
                    co_located_with="skill:media_center_skill",
                ),
            ),
            compatibility=deployment_sdk.DeploymentCompatibilityPolicy(
                minimum_runtime_version="0.1.917",
                required_protocols={"project_activation": "1", "distributed_topology": "1"},
                allow_release_skew=allow_release_skew,
            ),
            rollout=deployment_sdk.RolloutPolicy(
                batch_size=1,
                max_unavailable=1,
                stop_on_failure=True,
                rollback_on_failure=True,
            ),
            retention=deployment_sdk.DataRetentionPolicy(
                runtime_data="retain",
                derived_data="retain",
                external_data="retain",
            ),
            status="planned",
        )
        saved = deployment_sdk.define(
            desired,
            expected_revision=max(0, int(expected_revision)),
            reason=str(reason or "Media Center placement update"),
        )
        plan = deployment_sdk.plan(saved.deployment_id)
        return {
            "ok": plan.status == "ready",
            "schema": "adaos.media_center.deployment_plan.v1",
            "deployment": saved.to_dict(),
            "plan": plan.to_dict(),
            "dry_run": True,
        }

    def apply_deployment(self, plan_digest: str, *, idempotency_key: str = "") -> dict[str, Any]:
        operation = deployment_sdk.submit(
            plan_digest,
            idempotency_key=idempotency_key or f"media-center-apply-{uuid4().hex}",
            approvals=("remote_install",),
        )
        return {
            "ok": operation.state in {"accepted", "running", "succeeded", "partial"},
            "accepted": operation.state in {"accepted", "running"},
            "operation": operation.to_dict(),
        }

    @staticmethod
    def deployment_operation_status(operation_id: str) -> dict[str, Any]:
        operation = deployment_sdk.get_operation(str(operation_id or "").strip())
        return {
            "ok": True,
            "schema": "adaos.media_center.deployment_operation.v1",
            "operation": operation.to_dict(),
        }

    def drain_activation(self, activation_id: str, *, idempotency_key: str = "") -> dict[str, Any]:
        operation = deployment_sdk.drain(
            activation_id,
            idempotency_key=idempotency_key or f"media-center-drain-{uuid4().hex}",
        )
        return {"ok": operation.state in {"succeeded", "partial"}, "operation": operation.to_dict()}

    def remove_activation(self, activation_id: str, *, idempotency_key: str = "") -> dict[str, Any]:
        operation = deployment_sdk.remove(
            activation_id,
            idempotency_key=idempotency_key or f"media-center-remove-{uuid4().hex}",
        )
        return {"ok": operation.state in {"succeeded", "partial"}, "operation": operation.to_dict()}

    def define_topology(
        self,
        *,
        service_definition: Mapping[str, Any],
        service_group: Mapping[str, Any],
        datasets: list[Mapping[str, Any]],
        expected_group_revision: int = 0,
        deployment_id: str = DEPLOYMENT_ID,
    ) -> dict[str, Any]:
        requested_definition = distributed_sdk.ServiceDefinition.from_mapping(
            service_definition
        )
        requested_group = distributed_sdk.ServiceGroup.from_mapping(service_group)
        requested = [
            distributed_sdk.Dataset.from_mapping(raw) for raw in datasets[:20]
        ]
        if (
            requested_group.definition_id != requested_definition.definition_id
            or requested_group.definition_version != requested_definition.version
        ):
            raise RuntimeError("media_center_definition_group_mismatch")

        deployment = _dict(
            deployment_sdk.inspect(str(deployment_id or DEPLOYMENT_ID), limit=1)
        )
        desired = _dict(deployment.get("desired"))
        deployed_release = str(desired.get("release_digest") or "").strip()
        if not deployed_release:
            raise RuntimeError("media_center_deployment_release_unavailable")
        if requested_definition.release_digest != deployed_release:
            raise RuntimeError(
                "media_center_topology_release_mismatch:"
                f"expected={deployed_release}:"
                f"requested={requested_definition.release_digest}"
            )

        expected_group = max(0, int(expected_group_revision))
        current_group = None
        group_cursor: str | None = None
        while current_group is None:
            inspection = distributed_sdk.inspect(
                cursors={"groups": group_cursor} if group_cursor else None,
                limit=100,
            )
            current_group = next(
                (
                    item
                    for item in inspection.groups
                    if item.group_id == requested_group.group_id
                ),
                None,
            )
            group_cursor = inspection.cursors.get("groups")
            if current_group is not None or not group_cursor:
                break
        observed_group_revision = (
            0 if current_group is None else current_group.desired_revision
        )
        if expected_group != observed_group_revision:
            raise RuntimeError(
                "media_center_group_revision_conflict:"
                f"{requested_group.group_id}:expected={expected_group}:"
                f"observed={observed_group_revision}"
            )
        group_unchanged = (
            current_group is not None
            and current_group.to_dict() == requested_group.to_dict()
        )
        if (
            current_group is not None
            and current_group.definition_version != requested_definition.version
        ):
            current_definition = distributed_sdk.get_service_definition(
                current_group.definition_id,
                current_group.definition_version,
            )
            if (
                current_definition.release_digest
                != requested_definition.release_digest
                and not requested_definition.accepts_release(
                    current_definition.release_digest
                )
            ):
                raise RuntimeError(
                    "media_center_topology_release_overlap_required:"
                    f"current={current_definition.release_digest}:"
                    f"requested={requested_definition.release_digest}"
                )
        if not group_unchanged:
            if requested_group.desired_revision != observed_group_revision + 1:
                raise RuntimeError(
                    "media_center_group_revision_conflict:"
                    f"{requested_group.group_id}:next={observed_group_revision + 1}:"
                    f"requested={requested_group.desired_revision}"
                )
        wanted = {dataset.dataset_id for dataset in requested}
        existing: dict[str, Any] = {}
        cursor: str | None = None
        while wanted - existing.keys():
            inspection = distributed_sdk.inspect(
                cursors={"datasets": cursor} if cursor else None,
                limit=100,
            )
            for dataset in inspection.datasets:
                if dataset.dataset_id in wanted:
                    existing[dataset.dataset_id] = dataset
            cursor = inspection.cursors.get("datasets")
            if not cursor:
                break
        for dataset in requested:
            current = existing.get(dataset.dataset_id)
            if current is not None and current.to_dict() == dataset.to_dict():
                continue
            expected_revision = 0 if current is None else current.desired_revision
            if dataset.desired_revision != expected_revision + 1:
                raise RuntimeError(
                    "media_center_dataset_revision_conflict:"
                    f"{dataset.dataset_id}:expected={expected_revision + 1}:"
                    f"requested={dataset.desired_revision}"
                )

        definition = distributed_sdk.define_service(requested_definition)
        group = (
            current_group
            if group_unchanged
            else distributed_sdk.define_group(
                requested_group,
                expected_revision=expected_group,
            )
        )
        admitted = []
        for dataset in requested:
            current = existing.get(dataset.dataset_id)
            if current is not None and current.to_dict() == dataset.to_dict():
                admitted.append(current.to_dict())
                continue
            expected_revision = 0 if current is None else current.desired_revision
            admitted.append(
                distributed_sdk.define_dataset(
                    dataset,
                    expected_revision=expected_revision,
                ).to_dict()
            )
        return {"ok": True, "definition": definition.to_dict(), "group": group.to_dict(), "datasets": admitted}

    def register_agent(
        self,
        instance: Mapping[str, Any],
        *,
        expected_revision: int = 0,
        lease_seconds: int = 300,
    ) -> dict[str, Any]:
        registered = distributed_sdk.register(
            distributed_sdk.ServiceInstance.from_mapping(instance),
            expected_revision=max(0, int(expected_revision)),
            lease_seconds=max(30, min(int(lease_seconds), 600)),
        )
        return {"ok": True, "instance": registered.to_dict()}

    def renew_agent(
        self,
        instance_id: str,
        *,
        expected_revision: int,
        readiness: bool,
        status: str,
        health: Mapping[str, Any],
        pressure: Mapping[str, Any],
        lease_seconds: int = 300,
    ) -> dict[str, Any]:
        observed = distributed_sdk.renew(
            instance_id,
            expected_revision=max(1, int(expected_revision)),
            readiness=bool(readiness),
            status=status,
            health=health,
            pressure=pressure,
            lease_seconds=max(30, min(int(lease_seconds), 600)),
        )
        return {"ok": True, "instance": observed.to_dict()}

    def drain_agent(self, instance_id: str, *, expected_revision: int) -> dict[str, Any]:
        observed = distributed_sdk.drain(
            instance_id,
            expected_revision=max(1, int(expected_revision)),
        )
        return {"ok": True, "instance": observed.to_dict()}

    def observe_agent_topology(
        self,
        instance_id: str,
        *,
        partition: Mapping[str, Any],
        replica: Mapping[str, Any],
        timeout_seconds: float = 60.0,
    ) -> dict[str, Any]:
        reported = self.invoke_agent(
            instance_id,
            "observe_topology",
            {"partition": dict(partition), "replica": dict(replica)},
            timeout_seconds=timeout_seconds,
        )
        if reported.get("ok") is not True:
            raise RuntimeError(
                str(reported.get("error") or "media_agent_topology_observation_failed")
            )
        partition_value = distributed_sdk.Partition.from_mapping(
            reported.get("partition") or {}
        )
        replica_value = distributed_sdk.Replica.from_mapping(
            reported.get("replica") or {}
        )
        current_partition = self._partition(partition_value.partition_id)
        partition_payload = partition_value.to_dict()
        if current_partition is None:
            partition_payload["revision"] = 1
            saved_partition = distributed_sdk.put_partition(
                distributed_sdk.Partition.from_mapping(partition_payload),
                expected_revision=0,
            )
        else:
            current_payload = current_partition.to_dict()
            comparable_candidate = dict(partition_payload)
            comparable_current = dict(current_payload)
            comparable_candidate.pop("revision", None)
            comparable_current.pop("revision", None)
            if comparable_candidate == comparable_current:
                saved_partition = current_partition
            else:
                partition_payload["revision"] = current_partition.revision + 1
                saved_partition = distributed_sdk.put_partition(
                    distributed_sdk.Partition.from_mapping(partition_payload),
                    expected_revision=current_partition.revision,
                )

        current_replica = self._replica(replica_value.replica_id)
        replica_payload = replica_value.to_dict()
        replica_payload["revision"] = (
            1 if current_replica is None else current_replica.revision + 1
        )
        saved_replica = distributed_sdk.observe_replica(
            distributed_sdk.Replica.from_mapping(replica_payload),
            expected_revision=0 if current_replica is None else current_replica.revision,
        )
        return {
            "ok": True,
            "partition": saved_partition.to_dict(),
            "replica": saved_replica.to_dict(),
            "external_media_copied": bool(reported.get("external_media_copied")),
        }

    @staticmethod
    def _partition(partition_id: str) -> Any | None:
        cursor: str | None = None
        while True:
            inspection = distributed_sdk.inspect(
                cursors={"partitions": cursor} if cursor else None,
                limit=100,
            )
            current = next(
                (
                    item
                    for item in inspection.partitions
                    if item.partition_id == partition_id
                ),
                None,
            )
            if current is not None:
                return current
            cursor = inspection.cursors.get("partitions")
            if not cursor:
                return None

    @staticmethod
    def _replica(replica_id: str) -> Any | None:
        cursor: str | None = None
        while True:
            inspection = distributed_sdk.inspect(
                cursors={"replicas": cursor} if cursor else None,
                limit=100,
            )
            current = next(
                (item for item in inspection.replicas if item.replica_id == replica_id),
                None,
            )
            if current is not None:
                return current
            cursor = inspection.cursors.get("replicas")
            if not cursor:
                return None

    def plan_topology_change(
        self,
        partition_id: str,
        *,
        action: str,
        source_instance_id: str = "",
        target_instance_id: str = "",
        replica_role: str = "follower",
    ) -> dict[str, Any]:
        plan = distributed_sdk.plan_replica_change(
            partition_id,
            action=action,
            source_instance_id=source_instance_id or None,
            target_instance_id=target_instance_id or None,
            replica_role=replica_role,
        )
        return {"ok": plan.status == "ready", "dry_run": True, "plan": plan.to_dict()}

    def apply_topology_change(
        self,
        plan_digest: str,
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        plan = distributed_sdk.get_plan(plan_digest)
        operation = distributed_sdk.apply_plan(
            plan_digest,
            idempotency_key=idempotency_key or f"media-center-topology-{uuid4().hex}",
            approvals=tuple(plan.required_approvals),
        )
        return {"ok": operation.state == "succeeded", "operation": operation.to_dict()}

    @staticmethod
    def topology_operation_status(operation_id: str) -> dict[str, Any]:
        operation = distributed_sdk.get_operation(str(operation_id or "").strip())
        return {"ok": True, "operation": operation.to_dict()}

    def handoff_authority(
        self,
        partition_id: str,
        target_instance_id: str,
        *,
        expected_partition_revision: int,
        expected_epoch: int,
        operation_id: str = "",
        lease_seconds: int = 120,
    ) -> dict[str, Any]:
        lease = distributed_sdk.handoff_authority(
            partition_id,
            target_instance_id,
            expected_partition_revision=max(1, int(expected_partition_revision)),
            expected_epoch=max(0, int(expected_epoch)),
            operation_id=operation_id or f"media-center-handoff-{uuid4().hex}",
            lease_seconds=max(30, min(int(lease_seconds), 300)),
        )
        return {"ok": True, "lease": lease.to_dict()}

    def distributed_status(self, *, limit: int = 50) -> dict[str, Any]:
        try:
            bounded = max(1, min(int(limit), 100))
            inspection = distributed_sdk.inspect(limit=bounded)
            definitions: list[dict[str, Any]] = []
            definition_errors: list[dict[str, str]] = []
            seen: set[tuple[str, str]] = set()
            for group in inspection.groups[:bounded]:
                identity = (group.definition_id, group.definition_version)
                if identity in seen:
                    continue
                seen.add(identity)
                try:
                    definition = distributed_sdk.get_service_definition(*identity)
                    definitions.append(definition.to_dict())
                except Exception as exc:
                    definition_errors.append(
                        {
                            "definition_id": identity[0],
                            "version": identity[1],
                            "reason": str(exc)[:200],
                        }
                    )
            return {
                "ok": True,
                **inspection.to_dict(),
                "definitions": definitions,
                "definition_errors": definition_errors,
                "partial": bool(definition_errors),
            }
        except Exception as exc:
            return {
                "ok": False,
                "schema": "adaos.media_center.topology_status.v1",
                "state": "unavailable",
                "reason": str(exc)[:300],
            }

    def explain_route(
        self,
        partition_ids: list[str],
        *,
        dataset_id: str = "media-catalog-authority",
    ) -> dict[str, Any]:
        selected_dataset = str(dataset_id or "media-catalog-authority").strip()
        if selected_dataset not in MEDIA_DATASET_IDS:
            raise ValueError("media_center_dataset_not_supported")
        return distributed_sdk.explain_route(selected_dataset, partition_ids[:100])
