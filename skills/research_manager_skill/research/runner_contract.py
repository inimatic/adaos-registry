"""Canonical consumer ABI for direction-owned research runners."""

from __future__ import annotations

from typing import Any

from research.contracts import digest


def _sha256_schema() -> dict[str, Any]:
    return {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}


def _content_ref_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["uri", "digest", "size_bytes", "media_type", "owner_ref"],
        "properties": {
            "uri": {"type": "string", "minLength": 1},
            "digest": _sha256_schema(),
            "size_bytes": {"type": "integer", "minimum": 0},
            "media_type": {"type": "string", "minLength": 1},
            "owner_ref": {"type": "string", "pattern": "^skill:[A-Za-z0-9_.-]+$"},
            "kind": {"type": "string", "minLength": 1},
            "metadata": {"type": "object"},
        },
        "additionalProperties": True,
    }


def _collected_artifact_schema() -> dict[str, Any]:
    content_ref = _content_ref_schema()
    return {
        **content_ref,
        "required": [*content_ref["required"], "role"],
        "properties": {
            **content_ref["properties"],
            "role": {"type": "string", "minLength": 1},
        },
    }


def _observation_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["metric", "value"],
        "properties": {
            "metric": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "namespace": {"type": "string", "minLength": 1},
                    "name": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            },
            "value": {},
            "value_type": {
                "enum": [
                    "float",
                    "integer",
                    "boolean",
                    "string",
                    "vector",
                    "table",
                    "distribution",
                ]
            },
            "unit": {"type": "string"},
            "direction": {"type": "string"},
            "split_role": {
                "enum": ["train", "validation", "robustness", "test", "system"]
            },
            "dataset_digest": {
                "anyOf": [_sha256_schema(), {"type": "null"}],
            },
            "step": {
                "type": "object",
                "required": ["axis", "value"],
                "properties": {
                    "axis": {"type": "string", "minLength": 1},
                    "value": {},
                },
                "additionalProperties": True,
            },
            "aggregation": {"type": "string"},
            "observed_at": {"type": "string"},
            "producer": {"type": "object"},
            "evidence_role": {"type": "string"},
            "event_id": {"type": "string", "minLength": 1},
        },
        "additionalProperties": True,
    }


def _result_record_schema() -> dict[str, Any]:
    """Canonical scalar result consumed by ResearchManager.

    ExperimentPlan has exposed these exact paths since schema 1.1, but runner
    ABI 1.11 only typed observations and artifacts.  A provider could therefore
    report ``complete=true`` without returning the record used to construct a
    paired result.  Keep the record deliberately small and domain-neutral: the
    accepted ExperimentPlan supplies the scientific meaning of the scalar.
    """

    return {
        "type": "object",
        "required": [
            "primary_metric",
            "step",
            "pairing_identity_digest",
            "arm_id",
            "seed",
            "evidence_class",
        ],
        "properties": {
            "primary_metric": {"type": "number"},
            "step": {"type": "integer", "minimum": 0},
            "pairing_identity_digest": _sha256_schema(),
            "arm_id": {"type": "string", "minLength": 1},
            "seed": {"type": "integer"},
            "evidence_class": {"enum": ["workflow_smoke", "confirmatory"]},
        },
        "additionalProperties": True,
    }


def _split_binding_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["digest", "dataset_digest", "locator", "sealed"],
        "properties": {
            "digest": _sha256_schema(),
            "dataset_digest": _sha256_schema(),
            "locator": {"type": "string", "minLength": 1},
            "sealed": {"type": "boolean"},
        },
        "additionalProperties": True,
    }


def _workflow_smoke_documents() -> dict[str, Any]:
    """Return the public, machine-checkable evidence boundary for CPU smoke.

    These documents used to be known only to the independent evaluator.  That
    made conformance depend on guessing filenames and seed-label conventions.
    Keeping their schemas in the consumer ABI lets a generated provider target
    the contract while the evaluator still withholds its implementation and
    verdict logic.
    """

    seed_label = {"type": "string", "pattern": "^seed--?[0-9]+$"}
    indexed_content_ref = _content_ref_schema()
    indexed_content_ref = {
        **indexed_content_ref,
        "required": [*indexed_content_ref["required"], "metadata"],
        "properties": {
            **indexed_content_ref["properties"],
            "metadata": {
                "type": "object",
                "required": ["evidence_class"],
                "properties": {
                    "evidence_class": {"const": "workflow_smoke"},
                },
                "additionalProperties": True,
            },
        },
    }
    return {
        "required_expected_outputs": [
            "run_log.json",
            "evaluation_audit.json",
            "implementation_observation.json",
            "result_record.json",
            "artifacts_index.json",
        ],
        "documents": {
            "run_log.json": {
                "type": "object",
                "required": [
                    "stage",
                    "device",
                    "epochs_completed",
                    "seeds",
                    "inference_allowed",
                    "evidence_class",
                    "workload",
                    "input_policy",
                    "network",
                ],
                "properties": {
                    "stage": {"const": "workflow_smoke"},
                    "device": {"const": "cpu"},
                    "epochs_completed": {"const": 3},
                    "seeds": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": seed_label,
                    },
                    "inference_allowed": {"const": False},
                    "evidence_class": {"const": "workflow_smoke"},
                    "workload": {
                        "type": "object",
                        "required": ["mode", "limits", "observed"],
                        "properties": {
                            "mode": {"enum": ["bounded", "full"]},
                            "limits": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["name", "maximum", "unit"],
                                    "properties": {
                                        "name": {"type": "string", "minLength": 1},
                                        "maximum": {"type": "integer", "minimum": 1},
                                        "unit": {"type": "string", "minLength": 1},
                                    },
                                    "additionalProperties": False,
                                },
                            },
                            "observed": {
                                "type": "object",
                                "additionalProperties": {"type": "integer", "minimum": 0},
                            },
                        },
                        "additionalProperties": False,
                    },
                    "input_policy": {
                        "type": "object",
                        "required": ["source", "readiness", "sampling"],
                        "properties": {
                            "source": {"enum": ["accepted_dataset", "deterministic_contract_fixture"]},
                            "readiness": {"enum": ["required_before_execution", "may_prepare_during_execution"]},
                            "sampling": {"enum": ["deterministic_prefix", "deterministic_seeded", "full"]},
                        },
                        "additionalProperties": False,
                    },
                    "network": {
                        "type": "object",
                        "required": ["mode", "accessed"],
                        "properties": {
                            "mode": {"enum": ["offline", "unrestricted"]},
                            "accessed": {"type": "boolean"},
                        },
                        "additionalProperties": False,
                    },
                },
                "additionalProperties": True,
            },
            "evaluation_audit.json": {
                "type": "object",
                "required": ["per_stage", "test_access"],
                "properties": {
                    "per_stage": {
                        "type": "object",
                        "required": ["workflow_smoke"],
                        "properties": {
                            "workflow_smoke": {
                                "type": "object",
                                "required": ["test_evaluations_count"],
                                "properties": {
                                    "test_evaluations_count": {"const": 0},
                                },
                                "additionalProperties": True,
                            }
                        },
                        "additionalProperties": True,
                    },
                    "test_access": {"type": "array", "maxItems": 0},
                },
                "additionalProperties": True,
            },
            "implementation_observation.json": {
                "type": "object",
                "required": [
                    "schema",
                    "experiment_plan_digest",
                    "system_digest",
                    "arm",
                    "execution_path_digest",
                    "implementation",
                    "observed",
                ],
                "properties": {
                    "schema": {"const": "adaos.research.implementation_observation.v1"},
                    "experiment_plan_digest": _sha256_schema(),
                    "system_digest": _sha256_schema(),
                    "arm": {
                        "type": "object",
                        "required": ["id", "role"],
                        "properties": {
                            "id": {"type": "string", "minLength": 1},
                            "role": {"enum": ["baseline", "intervention"]},
                        },
                        "additionalProperties": True,
                    },
                    "execution_path_digest": _sha256_schema(),
                    "implementation": {
                        "type": "object",
                        "required": ["source_files", "callables"],
                        "properties": {
                            "source_files": {
                                "type": "array",
                                "minItems": 1,
                                "uniqueItems": True,
                                "items": {
                                    "type": "object",
                                    "required": ["path", "digest"],
                                    "properties": {
                                        "path": {"type": "string", "minLength": 1},
                                        "digest": _sha256_schema(),
                                    },
                                    "additionalProperties": False,
                                },
                            },
                            "callables": {
                                "type": "object",
                                "minProperties": 1,
                                "additionalProperties": {
                                    "type": "string",
                                    "minLength": 1,
                                },
                            },
                        },
                        "additionalProperties": True,
                    },
                    "observed": {"type": "object", "minProperties": 1},
                },
                "additionalProperties": True,
            },
            "result_record.json": {
                "type": "object",
                "required": [
                    "status",
                    "result",
                    "observations",
                    "evidence_class",
                    "tracker_session_calls",
                ],
                "properties": {
                    "status": {"const": "completed"},
                    "result": _result_record_schema(),
                    "observations": {
                        "type": "array",
                        "minItems": 1,
                        "items": _observation_schema(),
                    },
                    "evidence_class": {"const": "workflow_smoke"},
                    "tracker_session_calls": {"const": 0},
                },
                "additionalProperties": True,
            },
            "artifacts_index.json": {
                "type": "object",
                "required": ["files"],
                "properties": {
                    "files": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "required": ["path", "digest", "content_ref"],
                            "properties": {
                                "path": {"type": "string", "minLength": 1},
                                "digest": _sha256_schema(),
                                "content_ref": indexed_content_ref,
                            },
                            "additionalProperties": True,
                        },
                    }
                },
                "additionalProperties": True,
            },
        },
        "collection": {
            "tracker_session_calls": {"const": 0},
            "complete": {"const": True},
            "exact_artifact_set": {
                "authority": "artifacts_index.json.files",
                "collection": "collect_attempt.artifacts",
                "identity_key": "digest",
                "relation": "set_equal",
                "unique": True,
                "excluded_paths": ["artifacts_index.json"],
                "canonical_example": {
                    "index_paths": ["run_log.json", "evaluation_audit.json"],
                    "collected_paths": ["run_log.json", "evaluation_audit.json"],
                },
            },
            "artifact_identity": (
                "each artifacts_index entry must resolve to one trial output and one "
                "collect_attempt ContentRef with the same SHA-256 digest"
            ),
            "index_boundary": (
                "artifacts_index.json indexes collected evidence artifacts and MUST "
                "NOT index itself; self-indexing would require an impossible stable "
                "digest of a document that contains its own digest"
            ),
        },
        "canonicalization": {
            "rng_seed_type": "integer",
            "pairing_unit_id": "seed-{seed}",
            "example": {"seed": 17, "pairing_unit_id": "seed-17"},
            "execution_path_digest": (
                "sha256 of UTF-8 canonical JSON for implementation_observation.json.implementation; "
                "canonical JSON uses ensure_ascii=false, lexicographically sorted object keys, "
                "and separators ',' and ':'"
            ),
        },
    }


def descriptor() -> dict[str, Any]:
    workflow_smoke_evidence = _workflow_smoke_documents()
    value: dict[str, Any] = {
        "schema": "adaos.contract.operation_set.v1",
        "contract": "adaos.research.runner.v1",
        "version": "1.12.0",
        "consumer_ref": "skill:research_manager_skill",
        "capability": "research.runner",
        "operations": {
            "dataset_status": {
                "input_schema": {"type": "object", "additionalProperties": False},
                "output_required": ["dataset_id", "ready", "execution_ready_without_network", "split_bindings"],
                "output_schema": {
                    "type": "object",
                    "required": ["dataset_id", "ready", "execution_ready_without_network", "split_bindings"],
                    "properties": {
                        "dataset_id": {"type": "string", "minLength": 1},
                        "ready": {"type": "boolean"},
                        "execution_ready_without_network": {"type": "boolean"},
                        "split_bindings": {
                            "type": "object",
                            "required": ["validation", "robustness", "test"],
                            "properties": {
                                role: _split_binding_schema()
                                for role in ("validation", "robustness", "test")
                            },
                            "additionalProperties": False,
                        },
                    },
                    "additionalProperties": True,
                },
                "split_roles": ["validation", "robustness", "test"],
                "split_identity_fields": ["digest", "dataset_digest", "locator", "sealed"],
                "invariants": [
                    "all split digests are distinct SHA-256 identities",
                    "all split bindings share one dataset_digest",
                    "the test split is sealed",
                    "dataset_status is observational and performs no acquisition",
                    "execution_ready_without_network is true only when the selected smoke input can run without egress",
                ],
            },
            "prepare_attempt": {
                "input_schema": {
                    "type": "object",
                    "required": ["request"],
                    "properties": {
                        "request": {
                            "type": "object",
                            "required": [
                                "experiment_id",
                                "experiment_revision_id",
                                "trial_id",
                                "run_id",
                                "attempt_number",
                                "profile",
                                "seed",
                                "arm",
                                "conditions",
                                "profile_conditions",
                            ],
                            "properties": {
                                "experiment_id": {"type": "string", "minLength": 1},
                                "experiment_revision_id": {"type": "string", "minLength": 1},
                                "trial_id": {"type": "string", "minLength": 1},
                                "run_id": {"type": "string", "minLength": 1},
                                "attempt_number": {"type": "integer", "minimum": 1},
                                "profile": {
                                    "enum": ["preflight", "confirmatory"],
                                    "description": (
                                        "ResearchManager lifecycle profile. Use preflight "
                                        "for workflow_smoke evidence and confirmatory for "
                                        "confirmatory evidence; scientific stage identity is "
                                        "carried separately by profile_conditions.source_stage_id."
                                    ),
                                },
                                "seed": {"type": "integer"},
                                "arm": {
                                    "type": "object",
                                    "required": ["id", "role"],
                                    "properties": {
                                        "id": {"type": "string", "minLength": 1},
                                        "role": {
                                            "enum": ["baseline", "intervention"],
                                        },
                                        "label": {"type": "string"},
                                        "specification": {"type": "string"},
                                    },
                                    "additionalProperties": True,
                                },
                                "conditions": {
                                    "type": "object",
                                    "required": [
                                        "dataset",
                                        "operators",
                                        "execution",
                                        "randomization",
                                        "analysis",
                                        "tracker",
                                        "runner",
                                    ],
                                    "additionalProperties": True,
                                },
                                "profile_conditions": {
                                    "type": "object",
                                    "required": [
                                        "source_stage_id",
                                        "epochs",
                                        "seeds",
                                        "device",
                                        "evidence_class",
                                        "inference_allowed",
                                        "network_mode",
                                        "workload",
                                        "input_policy",
                                    ],
                                    "properties": {
                                        "source_stage_id": {"type": "string", "minLength": 1},
                                        "epochs": {"type": "integer", "minimum": 1},
                                        "seeds": {
                                            "type": "array",
                                            "items": {"type": "integer"},
                                            "minItems": 1,
                                        },
                                        "device": {"type": "string", "minLength": 1},
                                        "evidence_class": {
                                            "enum": ["workflow_smoke", "confirmatory"],
                                        },
                                        "inference_allowed": {"type": "boolean"},
                                        "network_mode": {"enum": ["offline", "unrestricted"]},
                                        "workload": {"type": "object"},
                                        "input_policy": {"type": "object"},
                                    },
                                    "additionalProperties": True,
                                },
                            },
                            "additionalProperties": True,
                        }
                    },
                    "additionalProperties": False,
                },
                "output_required": [
                    "contract",
                    "provider_id",
                    "package_ref",
                    "code_digest",
                    "environment_digest",
                    "spec_id",
                    "command",
                    "working_directory",
                    "output_ref",
                    "expected_outputs",
                ],
                "output_schema": {
                    "type": "object",
                    "required": [
                        "contract",
                        "provider_id",
                        "package_ref",
                        "code_digest",
                        "environment_digest",
                        "spec_id",
                        "command",
                        "working_directory",
                        "output_ref",
                        "expected_outputs",
                    ],
                    "properties": {
                        "contract": {"const": "adaos.research.runner.v1"},
                        "provider_id": {"type": "string", "minLength": 1},
                        "package_ref": _content_ref_schema(),
                        "code_digest": _sha256_schema(),
                        "environment_digest": _sha256_schema(),
                        "spec_id": {"type": "string", "minLength": 1},
                        "command": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                            "minItems": 2,
                        },
                        "working_directory": {"type": "string", "minLength": 1},
                        "output_ref": {"type": "string", "minLength": 1},
                        "expected_outputs": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                            "minItems": 1,
                        },
                        "environment": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                    },
                    "additionalProperties": True,
                },
                "execution_output_layout": {
                    "path_base": "working_directory",
                    "resolution": "Path(working_directory) / expected_outputs[i]",
                    "success_condition": "every resolved expected output is a regular file after command exit",
                    "subdirectory_policy": "encode every subdirectory explicitly in expected_outputs; an undeclared implicit outputs/ prefix is invalid",
                },
                "invariants": [
                    "contract equals adaos.research.runner.v1",
                    "provider_id equals the direction skill id",
                    "package_ref is a portable ContentRef owned by the direction skill",
                    "request.profile is exactly preflight when profile_conditions.evidence_class is workflow_smoke and exactly confirmatory when it is confirmatory; workflow_smoke is not a valid request.profile value",
                    "profile_conditions.source_stage_id carries the accepted scientific stage identity independently of the ResearchManager lifecycle profile",
                    "profile_conditions.input_policy is the sole input-source selector: deterministic_contract_fixture must execute the bounded production conformance path without opening the accepted dataset, while accepted_dataset selects the admitted scientific data path; providers must not require a private duplicate flag in conditions",
                    "arm is the exact accepted arm object; providers read arm.id instead of coercing the object to text",
                    "command[0] is the active Python interpreter and command[1] is an absolute runner path under the skill source",
                    "working_directory is a pre-created skill-owned execution-output directory",
                    "each expected_outputs[i] is a relative path resolved exactly as Path(working_directory) / expected_outputs[i]; writing it under an undeclared implicit outputs/ subdirectory is missing output even when command exit_code is zero",
                    "output_ref is an opaque portable key that collect_attempt resolves to that same attempt directory",
                    "conditions and profile_conditions are consumer authority; providers may validate them but must not replace their shape with a private one",
                    "a bounded workload applies every named maximum and reports observed units in run_log.json",
                    "offline execution starts only after dataset_status reports execution_ready_without_network=true and run_log.network.accessed remains false",
                    "preparation does not start scientific execution",
                    "expected_outputs contains every workflow_smoke_evidence.required_expected_outputs entry for a workflow-smoke request",
                    "implementation_observation.json is emitted by the same production path as the arm workload and binds the exact ExperimentPlan, system contract, arm and execution path",
                ],
                "profile_mapping": {
                    "preflight": {
                        "required_evidence_class": "workflow_smoke",
                        "scientific_stage_field": "profile_conditions.source_stage_id",
                        "inference_allowed": False,
                    },
                    "confirmatory": {
                        "required_evidence_class": "confirmatory",
                        "scientific_stage_field": "profile_conditions.source_stage_id",
                        "inference_allowed": True,
                    },
                },
            },
            "collect_attempt": {
                "input_schema": {
                    "type": "object",
                    "required": ["output_ref"],
                    "properties": {"output_ref": {"type": "string", "minLength": 1}},
                    "additionalProperties": False,
                },
                "output_required": [
                    "provider_id",
                    "observations",
                    "artifacts",
                    "result",
                    "complete",
                ],
                "output_schema": {
                    "type": "object",
                    "required": [
                        "provider_id",
                        "observations",
                        "artifacts",
                        "result",
                        "complete",
                    ],
                    "properties": {
                        "provider_id": {"type": "string", "minLength": 1},
                        "observations": {
                            "type": "array",
                            "items": _observation_schema(),
                        },
                        "artifacts": {
                            "type": "array",
                            "items": _collected_artifact_schema(),
                        },
                        "result": {
                            "anyOf": [_result_record_schema(), {"type": "null"}],
                        },
                        "complete": {"type": "boolean"},
                    },
                    "allOf": [
                        {
                            "if": {
                                "required": ["complete"],
                                "properties": {"complete": {"const": True}},
                            },
                            "then": {
                                "properties": {
                                    "observations": {"minItems": 1},
                                    "artifacts": {"minItems": 1},
                                    "result": _result_record_schema(),
                                }
                            },
                        }
                    ],
                    "additionalProperties": True,
                },
                "invariants": [
                    "provider_id equals the direction skill id",
                    "every observation is directly accepted by ResearchManager normalize_observation and supplies metric.name plus value",
                    "complete=true requires the canonical result record consumed by ExperimentPlan.runner_contract.result_record",
                    "result arm_id, seed and evidence_class equal the prepared request and result.primary_metric is repeated as a metric.name=primary_metric observation",
                    "workflow-smoke primary_metric is engineering evidence only and does not authorize scientific inference",
                    "artifacts contain portable content identities, never private host paths",
                    "every collected artifact supplies the non-empty ingestion role consumed by ResearchManager",
                    "workflow-smoke collection reports tracker_session_calls=0 because ResearchManager owns tracking",
                    "for workflow-smoke, artifact digests are unique and their exact set equals artifacts_index.json.files digests; artifacts_index.json itself is excluded from both sets",
                ],
            },
            "verify_artifact": {
                "input_schema": {
                    "type": "object",
                    "required": ["uri", "digest"],
                    "properties": {
                        "uri": {"type": "string", "minLength": 1},
                        "digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                    },
                    "additionalProperties": False,
                },
                "output_required": ["ok"],
                "output_schema": {
                    "type": "object",
                    "required": ["ok"],
                    "properties": {"ok": {"type": "boolean"}},
                    "additionalProperties": True,
                },
                "invariants": ["recompute or provider-verify the exact requested content identity"],
            },
        },
        "lifecycle": {
            "preparation": "direction skill",
            "submission": "AdaOS execution provider",
            "tracking": "research_manager_skill",
            "ingestion": "research_manager_skill",
            "scientific_smoke": "governed Study action after ProjectRelease",
        },
        "workflow_smoke_evidence": workflow_smoke_evidence,
        "conformance_fixtures": [
            {
                "id": "workflow_smoke.evidence_documents",
                "kind": "document_set",
                "required": True,
                "runtime_scope": "task_runtime",
                "selection": "newest_complete",
                "required_documents": list(
                    workflow_smoke_evidence["required_expected_outputs"]
                ),
                "documents": dict(workflow_smoke_evidence["documents"]),
            }
        ],
    }
    return {**value, "digest": digest(value)}


__all__ = ["descriptor"]
