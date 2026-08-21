from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

from adaos.services.traceability import build_graph, evaluate_paths

from research.contracts import digest, validate
from research.formulation import assemble_candidate, validate_stage


def _facet(stage: str, source_stage: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": "adaos.research.compilation_facet.v1",
        "stage": stage,
        "source_stage": source_stage,
        "payload": copy.deepcopy(dict(payload)),
    }
    value["digest"] = digest(value)
    return value


def _node_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_.:#/-]+", "-", str(value or "")).strip("-./:#")
    return token[:180] or "unknown"


def _build_experiment_plan(
    *,
    direction_id: str,
    task: Mapping[str, Any] | None,
    source_bundle_digest: str,
    protocol: Mapping[str, Any],
    compiled_evaluation: Mapping[str, Any],
) -> dict[str, Any]:
    """Compile scientific choices into the provider-neutral ResearchManager input.

    No runtime provider, data locator, or release identity is invented here.  Those
    are bound later by StudyRealization after the implementation is published.
    """

    experimental = dict(protocol["experimental_plan"])
    comparison = dict(experimental["comparison_design"])
    data_policy = dict(experimental["data_policy"])
    reproducibility = dict(experimental["reproducibility"])
    pairing = dict(reproducibility["pairing"])
    system = copy.deepcopy(dict(experimental["system_specification"]))
    system["digest"] = digest(system)
    evaluation = dict(compiled_evaluation)
    estimand = dict(evaluation["primary_estimand"])
    execution: dict[str, Any] = {}
    for stage in experimental["stages"]:
        stage_value = dict(stage)
        budget = dict(stage_value["budget"])
        profile = dict(stage_value["execution_profile"])
        stage_id = str(stage_value["id"])
        execution[stage_id] = {
            "stage_id": stage_id,
            "evidence_class": str(stage_value["evidence_class"]),
            "epochs": int(budget["epochs"]),
            "seeds": copy.deepcopy(list(budget["seed_values"])),
            "device": str(profile["device"]),
            "node": str(profile["node"]),
            "network_mode": str(profile["network_mode"]),
            "max_wall_time_minutes": int(budget["max_wall_time_minutes"]),
            "workload": copy.deepcopy(dict(budget["workload"])),
            "input_policy": copy.deepcopy(dict(stage_value["input_policy"])),
            "inference_allowed": bool(stage_value["inference_allowed"]),
            "stop_conditions": copy.deepcopy(list(stage_value["stop_conditions"])),
        }
    task_id = str((task or {}).get("task_id") or f"{direction_id}.task-001")
    plan: dict[str, Any] = {
        "schema": "adaos.research.experiment_plan.v1",
        "schema_version": "1.4.0",
        "direction_ref": f"research-direction:{direction_id}",
        "task_ref": str((task or {}).get("ref") or f"research-task:{task_id}"),
        "source_bundle_digest": str(source_bundle_digest),
        "dataset": {
            "id": str(data_policy["dataset_id"]),
            "logical_name": str(data_policy["dataset"]),
            "policy_digest": digest(data_policy),
            "split_strategy": str(data_policy["split_strategy"]),
            "evaluation_seal": str(data_policy["evaluation_seal"]),
            "leakage_controls": copy.deepcopy(list(data_policy["leakage_controls"])),
            "evaluation_access": copy.deepcopy(dict(data_policy["evaluation_access"])),
        },
        "operators": {"arms": copy.deepcopy(list(comparison["arms"]))},
        # This is the executable scientific subject, not review prose.  Earlier
        # projections retained arm labels but accidentally dropped the model,
        # input geometry, component settings and intervention boundary, which
        # allowed a bounded fixture to become a different scientific system.
        "system": system,
        "execution": execution,
        "randomization": {
            "paired": True,
            "unit": str(pairing["unit"]),
            "named_streams": [str(item["id"]) for item in reproducibility["rng_streams"]],
            "invariant_fields": copy.deepcopy(list(pairing["invariant_fields"])),
            "varied_fields": copy.deepcopy(list(pairing["varied_fields"])),
            "allocation": copy.deepcopy(dict(pairing["allocation"])),
        },
        "analysis": {
            "primary_metric": str(estimand["metric"]),
            "primary_estimand": str(estimand["name"]),
            "primary_contrast": copy.deepcopy(dict(comparison["primary_contrast"])),
            "uncertainty": copy.deepcopy(dict(evaluation["uncertainty"])),
            "stopping_rule": copy.deepcopy(dict(evaluation["stopping_rule"])),
            "decision_rules": copy.deepcopy(list(evaluation["decision_rules"])),
            "practical_significance": str(evaluation["practical_significance"]),
            "negative_result_policy": str(evaluation["negative_result_policy"]),
        },
        "evidence_policy": {
            "historical_results": "exploratory_source_only",
            "workflow_smoke": "workflow_evidence_only",
            "negative_results": "retain_and_report",
        },
        "runner_contract": {
            "contract": "adaos.research.runner.v1",
            "operations": ["prepare_attempt", "collect_attempt", "verify_artifact", "dataset_status"],
            "dataset_binding": {
                "required_roles": ["validation", "robustness", "test"],
                "test_sealed": True,
                "identity_fields": ["digest", "dataset_digest", "locator"],
            },
            "result_record": {
                "primary_metric_path": "primary_metric",
                "step_path": "step",
                "pairing_identity_path": "pairing_identity_digest",
                "arm_id_path": "arm_id",
                "seed_path": "seed",
                "evidence_class_path": "evidence_class",
            },
        },
    }
    plan["digest"] = digest(plan)
    return validate("research.experiment_plan.v1.schema.json", plan)


def build_compilation(
    *,
    direction_id: str,
    task: Mapping[str, Any] | None = None,
    run_id: str,
    source_bundle: Mapping[str, Any],
    source_context: Mapping[str, Any],
    problem_frame: Mapping[str, Any],
    protocol_design: Mapping[str, Any],
    implementation_contract: Mapping[str, Any],
    source_ref_map: Mapping[str, str],
    required_workflow_smoke: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile LLM stage products into one auditable, model-independent handoff."""

    problem = validate_stage("problem_frame", problem_frame)
    protocol = validate_stage("protocol_design", protocol_design)
    implementation = validate_stage("implementation_contract", implementation_contract)
    assembled = assemble_candidate(
        problem,
        protocol,
        implementation,
        source_ref_map=source_ref_map,
        required_workflow_smoke=required_workflow_smoke,
    )
    reference_map = {str(key): str(value) for key, value in source_ref_map.items()}

    coverage = copy.deepcopy(dict(source_context.get("coverage") or {}))
    coverage_by_ref = {
        str(item.get("artifact_ref") or ""): dict(item)
        for item in coverage.get("items") or []
        if isinstance(item, Mapping)
    }
    inventory = []
    for source in source_bundle.get("sources") or []:
        if not isinstance(source, Mapping):
            continue
        artifact_ref = str(source.get("artifact_ref") or "")
        extraction = coverage_by_ref.get(artifact_ref, {})
        inventory.append(
            {
                "source_id": str(source.get("source_id") or ""),
                "name": str(source.get("name") or ""),
                "digest": str(source.get("digest") or ""),
                "media_type": str(source.get("media_type") or "application/octet-stream"),
                "role": str(source.get("role") or "source"),
                "artifact_ref": artifact_ref,
                "analysis": copy.deepcopy(dict(source.get("analysis") or {})),
                "extraction": {
                    "strategy": str(extraction.get("strategy") or "unavailable"),
                    "selected_characters": int(extraction.get("selected_characters") or 0),
                    "truncated": bool(extraction.get("truncated")),
                    "provenance_refs": [str(item) for item in extraction.get("provenance_refs") or []],
                },
            }
        )
    assessment = problem["source_assessment"]

    def resolved_claims(values: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "claim": str(item["claim"]),
                "source_refs": list(
                    dict.fromkeys(
                        reference_map.get(str(ref), str(ref))
                        for ref in item.get("source_refs") or []
                    )
                ),
            }
            for item in values
        ]

    source_analysis = {
        "schema": "adaos.research.source_analysis.v1",
        "source_bundle_digest": str(source_bundle["digest"]),
        "inventory": inventory,
        "sufficiency": str(assessment["sufficiency"]),
        "observed_facts": resolved_claims(list(assessment["observed_facts"])),
        "author_interpretations": resolved_claims(list(assessment["author_interpretations"])),
        "coverage_limitations": [str(item) for item in assessment["coverage_limitations"]],
        "unresolved_decisions": [str(item) for item in assessment["unresolved_decisions"]],
    }
    source_analysis["digest"] = digest(source_analysis)
    research_problem = {
        key: copy.deepcopy(problem[key])
        for key in (
            "assistant_message",
            "title",
            "background",
            "research_question",
            "hypotheses",
            "constraints",
            "assumptions",
            "open_questions",
            "experimental_signature",
        )
    }
    experiment_plan = _build_experiment_plan(
        direction_id=direction_id,
        task=task,
        source_bundle_digest=str(source_bundle["digest"]),
        protocol=protocol,
        compiled_evaluation=dict(assembled["evaluation_plan"]),
    )
    facets = {
        "source_analysis": _facet("source_analysis", "problem_frame+deterministic_extraction", source_analysis),
        "research_problem": _facet("research_problem", "problem_frame", research_problem),
        "experimental_protocol": _facet("experimental_protocol", "protocol_design", protocol),
        "engineering_contract": _facet("engineering_contract", "implementation_contract", implementation),
        "experiment_plan": _facet("experiment_plan", "deterministic_protocol_compiler", experiment_plan),
    }

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    cited_short_refs: set[str] = set()
    for short, exact in sorted(reference_map.items()):
        nodes.append(
            {
                "node_id": f"source:{short}",
                "kind": "source_fragment",
                "ref": exact,
            }
        )
    hypothesis_nodes: list[str] = []
    for index, hypothesis in enumerate(problem["hypotheses"], start=1):
        hypothesis_id = _node_token(str(hypothesis.get("id") or f"H{index}"))
        node_id = f"hypothesis:{hypothesis_id}"
        hypothesis_nodes.append(node_id)
        nodes.append({"node_id": node_id, "kind": "hypothesis", "label": str(hypothesis["statement"])})
        for short in dict.fromkeys(str(item) for item in hypothesis.get("source_refs") or []):
            cited_short_refs.add(short)
            edges.append(
                {
                    "edge_id": f"grounding:{short}:{hypothesis_id}",
                    "source": f"source:{short}",
                    "target": node_id,
                    "relation": "motivates",
                }
            )
    protocol_node = "protocol:accepted-candidate"
    engineering_node = "engineering:compiled-contract"
    nodes.extend(
        [
            {"node_id": protocol_node, "kind": "experimental_protocol", "digest": facets["experimental_protocol"]["digest"]},
            {"node_id": engineering_node, "kind": "engineering_contract", "digest": facets["engineering_contract"]["digest"]},
        ]
    )
    for index, node_id in enumerate(hypothesis_nodes, start=1):
        edges.append(
            {
                "edge_id": f"operationalization:{index}",
                "source": node_id,
                "target": protocol_node,
                "relation": "operationalized_by",
            }
        )
    edges.append(
        {
            "edge_id": "compilation:protocol-to-engineering",
            "source": protocol_node,
            "target": engineering_node,
            "relation": "compiled_as",
        }
    )
    check_nodes: list[str] = []
    checks = [
        item
        for values in implementation["checks_by_category"].values()
        for item in values
    ]
    for index, check in enumerate(checks, start=1):
        node_id = f"acceptance:AC-{index}"
        check_nodes.append(node_id)
        nodes.append({"node_id": node_id, "kind": "acceptance_check", "label": str(check["check"])})
        edges.append(
            {
                "edge_id": f"verification:AC-{index}",
                "source": engineering_node,
                "target": node_id,
                "relation": "verified_by",
            }
        )
    graph = build_graph(
        f"research-compilation:{direction_id}:{run_id}",
        revision=1,
        nodes=nodes,
        edges=edges,
    )
    requirements = []
    for short in sorted(cited_short_refs):
        for index, check_node in enumerate(check_nodes, start=1):
            requirements.append(
                {
                    "requirement_id": f"{short}-to-AC-{index}",
                    "source": f"source:{short}",
                    "target": check_node,
                    "via_kinds": ["hypothesis", "experimental_protocol", "engineering_contract"],
                }
            )
    traceability_coverage = evaluate_paths(graph, requirements)
    blockers = []
    if int(coverage.get("sources_represented") or 0) != int(coverage.get("sources_total") or 0):
        blockers.append("source extraction does not represent every visible artifact")
    if not traceability_coverage["valid"]:
        blockers.append("source-to-acceptance traceability has missing paths")
    if not cited_short_refs:
        blockers.append("the primary hypothesis has no admitted source reference")
    package: dict[str, Any] = {
        "schema": "adaos.research.compilation_package.v1",
        "schema_version": "1.1.0",
        "direction_id": str(direction_id),
        "run_id": str(run_id),
        "source_bundle_digest": str(source_bundle["digest"]),
        "context_receipt": {
            "audience": "research.formulation",
            "visible_source_ids": sorted(str(item.get("source_id") or "") for item in source_bundle.get("sources") or []),
            "excluded_count": len(source_bundle.get("excluded") or []),
            "coverage": coverage,
        },
        "facets": facets,
        "traceability_graph": graph,
        "traceability_coverage": traceability_coverage,
        "readiness": {
            "decision": "needs_revision" if blockers else "ready_for_acceptance",
            "blockers": blockers,
        },
    }
    if task is not None:
        task_id = str(task.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("research compilation task requires task_id")
        if str(task.get("direction_id") or direction_id) != str(direction_id):
            raise ValueError("research compilation task belongs to another direction")
        package["task"] = {
            "task_id": task_id,
            "ref": str(task.get("ref") or f"research-task:{task_id}"),
            "revision": int(task.get("revision") or 1),
        }
    package["digest"] = digest(package)
    return validate("research.compilation_package.v1.schema.json", package)


def project_execution_compilation(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project the full audit package into the minimal scientific Codex input."""

    source = validate("research.compilation_package.v1.schema.json", value)
    source_analysis = dict(source["facets"]["source_analysis"]["payload"])
    problem = copy.deepcopy(dict(source["facets"]["research_problem"]["payload"]))
    problem.pop("assistant_message", None)
    protocol_facet = dict(source["facets"]["experimental_protocol"])
    protocol = copy.deepcopy(dict(protocol_facet["payload"]))
    protocol.pop("assistant_message", None)
    # The normalized experiment_plan facet is the executable authority. The
    # formulation facet keeps the same large plan only for audit/review, so
    # sending both to Codex adds attention cost without an additional
    # obligation or identity.
    if "experiment_plan" in source["facets"]:
        protocol.pop("experimental_plan", None)
    graph = dict(source["traceability_graph"])
    allowed_kinds = {"source_fragment", "hypothesis", "experimental_protocol"}
    nodes = [
        copy.deepcopy(dict(item))
        for item in graph.get("nodes") or []
        if str(item.get("kind") or "") in allowed_kinds
    ]
    admitted = {str(item.get("node_id") or "") for item in nodes}
    edges = [
        copy.deepcopy(dict(item))
        for item in graph.get("edges") or []
        if str(item.get("source") or "") in admitted and str(item.get("target") or "") in admitted
    ]
    projected = {
        "schema": "adaos.research.compilation_projection.v1",
        "schema_version": "1.3.0" if "experiment_plan" in source["facets"] else "1.0.0",
        "direction_id": source["direction_id"],
        "compilation_digest": source["digest"],
        "source_bundle_digest": source["source_bundle_digest"],
        "source_analysis": {
            key: copy.deepcopy(source_analysis.get(key))
            for key in (
                "sufficiency",
                "observed_facts",
                "author_interpretations",
                "coverage_limitations",
                "unresolved_decisions",
            )
        },
        "research_problem": problem,
        "experimental_protocol": protocol,
        **(
            {"experiment_plan": copy.deepcopy(source["facets"]["experiment_plan"]["payload"])}
            if "experiment_plan" in source["facets"]
            else {}
        ),
        "traceability": {
            "protocol_digest": protocol_facet["digest"],
            "nodes": nodes,
            "edges": edges,
        },
        "readiness": copy.deepcopy(source["readiness"]),
    }
    if source.get("task"):
        projected["task"] = copy.deepcopy(dict(source["task"]))
    projected["digest"] = digest(projected)
    return validate("research.compilation_projection.v1.schema.json", projected)


__all__ = ["build_compilation", "project_execution_compilation"]
