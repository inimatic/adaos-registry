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
        },
    }


def descriptor() -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": "adaos.contract.operation_set.v1",
        "contract": "adaos.research.runner.v1",
        "version": "1.3.0",
        "consumer_ref": "skill:research_manager_skill",
        "capability": "research.runner",
        "operations": {
            "dataset_status": {
                "input_schema": {"type": "object", "additionalProperties": False},
                "output_required": ["dataset_id", "split_bindings"],
                "output_schema": {
                    "type": "object",
                    "required": ["dataset_id", "split_bindings"],
                    "properties": {
                        "dataset_id": {"type": "string", "minLength": 1},
                        "ready": {"type": "boolean"},
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
                                "profile": {"type": "string", "minLength": 1},
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
                "invariants": [
                    "contract equals adaos.research.runner.v1",
                    "provider_id equals the direction skill id",
                    "package_ref is a portable ContentRef owned by the direction skill",
                    "profile is a ResearchManager lifecycle label; source_stage_id and evidence_class carry the accepted scientific stage semantics",
                    "arm is the exact accepted arm object; providers read arm.id instead of coercing the object to text",
                    "command[0] is the active Python interpreter and command[1] is an absolute runner path under the skill source",
                    "working_directory is a pre-created skill-owned attempt directory and every expected output is written beneath it",
                    "output_ref is an opaque portable key that collect_attempt resolves to that same attempt directory",
                    "conditions and profile_conditions are consumer authority; providers may validate them but must not replace their shape with a private one",
                    "preparation does not start scientific execution",
                    "expected_outputs contains every workflow_smoke_evidence.required_expected_outputs entry for a workflow-smoke request",
                ],
            },
            "collect_attempt": {
                "input_schema": {
                    "type": "object",
                    "required": ["output_ref"],
                    "properties": {"output_ref": {"type": "string", "minLength": 1}},
                    "additionalProperties": False,
                },
                "output_required": ["provider_id", "observations", "artifacts", "complete"],
                "output_schema": {
                    "type": "object",
                    "required": ["provider_id", "observations", "artifacts", "complete"],
                    "properties": {
                        "provider_id": {"type": "string", "minLength": 1},
                        "observations": {
                            "type": ["array", "object"],
                        },
                        "artifacts": {
                            "type": "array",
                            "items": _content_ref_schema(),
                        },
                        "complete": {"type": "boolean"},
                    },
                    "additionalProperties": True,
                },
                "invariants": [
                    "provider_id equals the direction skill id",
                    "observations use the canonical result_record paths",
                    "artifacts contain portable content identities, never private host paths",
                    "workflow-smoke collection reports tracker_session_calls=0 because ResearchManager owns tracking",
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
        "workflow_smoke_evidence": _workflow_smoke_documents(),
    }
    return {**value, "digest": digest(value)}


__all__ = ["descriptor"]
