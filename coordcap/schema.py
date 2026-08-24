"""Schemas and strict parsing helpers for CoordCap model outputs.

This module intentionally contains no evaluator logic.  In particular, the
``utility`` slot is a required JSON null placeholder; utility and regret are
computed only by the deterministic evaluator from a model's decision.
"""

from __future__ import annotations

import json
import copy
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator


METHODS = (
    "direct_joint_prompt",
    "sequential_aggregation",
    "constraint_ledger",
    "budget_aware_planner",
)
CALL_BUDGETS = (1, 2, 4)
ANSWER_TOKEN_BUDGETS = (512, 1024, 2048)


# Plan identity is the only decision primitive consumed by the deterministic
# evaluator.  Explanatory fields never override that selected plan.
FINAL_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "selected_plan",
        "principal_outcomes",
        "resource_allocation",
        "unresolved_conflicts",
        "abstain",
    ],
    "properties": {
        "selected_plan": {
            "type": "object",
            "additionalProperties": False,
            "required": ["plan_id"],
            "properties": {"plan_id": {"type": "string"}},
        },
        "principal_outcomes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "principal_id",
                    "satisfied_hard_constraints",
                    "violated_hard_constraints",
                    "utility",
                    "justification",
                ],
                "properties": {
                    "principal_id": {"type": "string"},
                    "satisfied_hard_constraints": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "violated_hard_constraints": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    # Models must never estimate or fill utility.
                    "utility": {"type": "null"},
                    "justification": {"type": "string"},
                },
            },
        },
        "resource_allocation": {"type": "object"},
        "unresolved_conflicts": {
            "type": "array",
            "items": {"type": "string"},
        },
        "abstain": {"type": "boolean"},
    },
}


ANALYSIS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "summary",
        "hard_constraints",
        "soft_preferences",
        "conflicts",
        "candidate_actions",
        "uncertainties",
    ],
    "properties": {
        "summary": {"type": "string"},
        "hard_constraints": {"type": "array", "items": {"type": "string"}},
        "soft_preferences": {"type": "array", "items": {"type": "string"}},
        "conflicts": {"type": "array", "items": {"type": "string"}},
        "candidate_actions": {"type": "array", "items": {"type": "string"}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
    },
}


_FINAL_VALIDATOR = Draft202012Validator(FINAL_RESPONSE_SCHEMA)
_ANALYSIS_VALIDATOR = Draft202012Validator(ANALYSIS_RESPONSE_SCHEMA)


def final_response_schema_for_task(public_task: Mapping[str, Any]) -> dict[str, Any]:
    """Close the task-specific resource object for strict structured output.

    Resource keys are public decision fields, not oracle information.  Their
    values remain model-authored diagnostics and never affect primary scoring.
    """

    schema = copy.deepcopy(FINAL_RESPONSE_SCHEMA)
    plans = public_task.get("plans")
    resource_key_sets: list[set[str]] = []
    if isinstance(plans, list):
        for plan in plans:
            if not isinstance(plan, Mapping):
                continue
            decision = plan.get("decision")
            allocation = (
                decision.get("resource_allocation")
                if isinstance(decision, Mapping)
                else None
            )
            if isinstance(allocation, Mapping):
                resource_key_sets.append({str(key) for key in allocation})
    if resource_key_sets and any(keys != resource_key_sets[0] for keys in resource_key_sets[1:]):
        raise ValueError("public plan catalogue has inconsistent resource-allocation keys")
    resource_keys = sorted(resource_key_sets[0]) if resource_key_sets else []
    schema["properties"]["resource_allocation"] = {
        "type": "object",
        "additionalProperties": False,
        "required": resource_keys,
        "properties": {key: {"type": "integer"} for key in resource_keys},
    }
    return schema


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate object key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")


def strict_json_loads(text: str) -> tuple[Any | None, str | None]:
    """Parse one complete JSON value without markdown or trailing text."""

    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return None, f"invalid_json: {exc}"
    return value, None


def schema_errors(
    value: Any,
    *,
    final: bool,
    schema: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return stable, human-readable validation errors."""

    validator = (
        Draft202012Validator(dict(schema))
        if schema is not None
        else _FINAL_VALIDATOR
        if final
        else _ANALYSIS_VALIDATOR
    )
    errors: list[str] = []
    for error in sorted(validator.iter_errors(value), key=lambda item: list(item.path)):
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        errors.append(f"{location}: {error.message}")
    return errors


def parse_and_validate(
    text: str,
    *,
    final: bool,
    schema: Mapping[str, Any] | None = None,
) -> tuple[Any | None, bool, list[str]]:
    """Strictly parse and validate a final or intermediate response."""

    value, parse_error = strict_json_loads(text)
    if parse_error is not None:
        return None, False, [parse_error]
    errors = schema_errors(value, final=final, schema=schema)
    return value, not errors, errors


def response_format(schema: dict[str, Any], *, name: str) -> dict[str, Any]:
    """Build an OpenAI/OpenRouter-compatible JSON-schema response format."""

    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": schema,
        },
    }
