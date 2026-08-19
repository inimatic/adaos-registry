from __future__ import annotations

from typing import Any, Mapping
from uuid import uuid4

from adaos.sdk import deployment as deployment_sdk
from adaos.sdk import distributed as distributed_sdk


DEPLOYMENT_ID = "media-center-home"


def _dict(value: Any) -> dict[str, Any]:
    method = getattr(value, "to_dict", None)
    return dict(method()) if callable(method) else dict(value or {})


class MediaCenterTopology:
    """Media policy projected over public deployment and distributed SDKs."""

    def deployment_status(self, deployment_id: str = DEPLOYMENT_ID, *, limit: int = 50) -> dict[str, Any]:
        try:
            inspection = deployment_sdk.inspect(deployment_id, limit=max(1, min(int(limit), 100)))
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
        nodes: dict[str, dict[str, Any]] = {}
        for activation in activations:
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
            row["components"].append(activation.get("component_ref"))
            row["generation"] = max(int(row["generation"]), int(activation.get("generation") or 0))
            row["agent"] = row["agent"] or activation.get("component_ref") == "skill:media_library_agent"
            if activation.get("status") not in {"active", "removed"}:
                row["state"] = activation.get("status")
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
            "operations": list(value.get("operations") or [])[:50],
            "next_activation_cursor": value.get("activation_cursor"),
            "next_operation_cursor": value.get("operation_cursor"),
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
                minimum_runtime_version="0.1.868",
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
        operation = deployment_sdk.apply(
            plan_digest,
            idempotency_key=idempotency_key or f"media-center-apply-{uuid4().hex}",
            approvals=("remote_install",),
        )
        return {"ok": operation.state in {"succeeded", "partial"}, "operation": operation.to_dict()}

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
    ) -> dict[str, Any]:
        definition = distributed_sdk.define_service(
            distributed_sdk.ServiceDefinition.from_mapping(service_definition)
        )
        group = distributed_sdk.define_group(
            distributed_sdk.ServiceGroup.from_mapping(service_group),
            expected_revision=max(0, int(expected_group_revision)),
        )
        admitted = []
        for raw in datasets[:20]:
            dataset = distributed_sdk.Dataset.from_mapping(raw)
            admitted.append(
                distributed_sdk.define_dataset(dataset, expected_revision=max(0, dataset.desired_revision - 1)).to_dict()
            )
        return {"ok": True, "definition": definition.to_dict(), "group": group.to_dict(), "datasets": admitted}

    def distributed_status(self, *, limit: int = 50) -> dict[str, Any]:
        try:
            inspection = distributed_sdk.inspect(limit=max(1, min(int(limit), 100)))
            return {"ok": True, **inspection.to_dict()}
        except Exception as exc:
            return {
                "ok": False,
                "schema": "adaos.media_center.topology_status.v1",
                "state": "unavailable",
                "reason": str(exc)[:300],
            }

    def explain_route(self, partition_ids: list[str]) -> dict[str, Any]:
        return distributed_sdk.explain_route("media-center-sources", partition_ids[:100])
