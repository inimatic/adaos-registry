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


def descriptor() -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": "adaos.contract.operation_set.v1",
        "contract": "adaos.research.runner.v1",
        "version": "1.2.0",
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
    }
    return {**value, "digest": digest(value)}


__all__ = ["descriptor"]
