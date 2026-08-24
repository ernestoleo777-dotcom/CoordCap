"""Primary exhaustive reference solver for public CoordCap tasks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .canonical import canonical_json


UTILITY_SCALE_BP = 1000


class OracleError(ValueError):
    """Raised when a public task cannot be solved exactly."""


def _nested_value(decision: Mapping[str, Any], dotted_field: str) -> Any:
    value: Any = decision
    parts = dotted_field.split(".")
    if parts and parts[0] == "decision":
        parts = parts[1:]
    for part in parts:
        if not isinstance(value, Mapping) or part not in value:
            raise OracleError(f"decision has no field {dotted_field!r}")
        value = value[part]
    return value


def _constraint_satisfied(decision: Mapping[str, Any], constraint: Mapping[str, Any]) -> bool:
    field = constraint.get("field")
    operator = constraint.get("operator")
    if not isinstance(field, str) or not isinstance(operator, str):
        raise OracleError("constraint field/operator must be strings")
    observed = _nested_value(decision, field)
    if operator == "equal":
        return observed == constraint.get("value")
    if operator == "not_equal":
        return observed != constraint.get("value")
    if operator == "one_of":
        allowed = constraint.get("values")
        if not isinstance(allowed, list):
            raise OracleError("one_of constraint requires values")
        return observed in allowed
    if operator == "not_in":
        forbidden = constraint.get("values")
        if not isinstance(forbidden, list):
            raise OracleError("not_in constraint requires values")
        return observed not in forbidden
    if operator == "at_least":
        return isinstance(observed, int) and observed >= constraint.get("value")
    if operator == "at_most":
        return isinstance(observed, int) and observed <= constraint.get("value")
    raise OracleError(f"unsupported constraint operator {operator!r}")


def _constraint_id(constraint: Mapping[str, Any], context: str) -> str:
    value = constraint.get("constraint_id")
    if not isinstance(value, str) or not value:
        raise OracleError(f"{context} has no constraint_id")
    return value


def _plan_id(plan: Mapping[str, Any]) -> str:
    value = plan.get("plan_id")
    if not isinstance(value, str) or not value:
        raise OracleError("candidate plan has no plan_id")
    return value


def _principal_id(principal: Mapping[str, Any]) -> str:
    value = principal.get("principal_id")
    if not isinstance(value, str) or not value:
        raise OracleError("principal has no principal_id")
    return value


def _soft_score(decision: Mapping[str, Any], principal: Mapping[str, Any]) -> int:
    preferences = principal.get("soft_preferences")
    if not isinstance(preferences, list) or not preferences:
        raise OracleError(f"principal {_principal_id(principal)} has no soft preferences")
    total_available = 0
    earned = 0
    for preference in preferences:
        if not isinstance(preference, Mapping):
            raise OracleError("soft preference must be an object")
        issue = preference.get("issue_id")
        desired = preference.get("desired_value")
        points = preference.get("points_bp")
        if not isinstance(issue, str) or isinstance(points, bool) or not isinstance(points, int):
            raise OracleError("invalid soft preference")
        if points < 0:
            raise OracleError("soft preference points must be nonnegative")
        total_available += points
        observed = (
            decision.get("assurance")
            if issue == "assurance"
            else _nested_value(decision, f"settings.{issue}")
        )
        if observed == desired:
            earned += points
    if total_available != UTILITY_SCALE_BP:
        raise OracleError(
            f"principal {_principal_id(principal)} soft points total {total_available}, expected 1000"
        )
    return earned


def _pareto_ids(
    feasible_ids: Sequence[str],
    scores: Mapping[str, Mapping[str, int]],
    principal_ids: Sequence[str],
) -> list[str]:
    frontier: list[str] = []
    for candidate in feasible_ids:
        dominated = False
        for challenger in feasible_ids:
            if challenger == candidate:
                continue
            weakly_better = all(
                scores[challenger][principal_id] >= scores[candidate][principal_id]
                for principal_id in principal_ids
            )
            strictly_better = any(
                scores[challenger][principal_id] > scores[candidate][principal_id]
                for principal_id in principal_ids
            )
            if weakly_better and strictly_better:
                dominated = True
                break
        if not dominated:
            frontier.append(candidate)
    return sorted(frontier)


def solve_public_task(public_task: Mapping[str, Any]) -> dict[str, Any]:
    """Exhaustively compute the complete integer oracle from public data only."""

    plans_raw = public_task.get("plans")
    principals_raw = public_task.get("principals")
    shared_raw = public_task.get("shared_constraints")
    if not isinstance(plans_raw, list) or not plans_raw:
        raise OracleError("public task must contain a finite plans array")
    if not isinstance(principals_raw, list) or len(principals_raw) < 2:
        raise OracleError("public task must contain at least two principals")
    if not isinstance(shared_raw, list) or not shared_raw:
        raise OracleError("public task must contain shared constraints")
    plans: dict[str, Mapping[str, Any]] = {}
    for plan in plans_raw:
        if not isinstance(plan, Mapping):
            raise OracleError("plan must be an object")
        plan_id = _plan_id(plan)
        if plan_id in plans:
            raise OracleError(f"duplicate plan ID {plan_id}")
        decision = plan.get("decision")
        if not isinstance(decision, Mapping):
            raise OracleError(f"plan {plan_id} has no decision object")
        plans[plan_id] = plan
    principals: dict[str, Mapping[str, Any]] = {}
    weights: dict[str, int] = {}
    for principal in principals_raw:
        if not isinstance(principal, Mapping):
            raise OracleError("principal must be an object")
        principal_id = _principal_id(principal)
        if principal_id in principals:
            raise OracleError(f"duplicate principal ID {principal_id}")
        weight = principal.get("priority_weight")
        if isinstance(weight, bool) or not isinstance(weight, int) or weight < 1:
            raise OracleError(f"principal {principal_id} has invalid priority weight")
        constraints = principal.get("hard_constraints")
        if not isinstance(constraints, list) or not constraints:
            raise OracleError(f"principal {principal_id} has no hard constraints")
        principals[principal_id] = principal
        weights[principal_id] = weight
    principal_ids = sorted(principals)
    shared_ids = [
        _constraint_id(constraint, "shared constraint")
        for constraint in shared_raw
        if isinstance(constraint, Mapping)
    ]
    if len(shared_ids) != len(shared_raw) or len(shared_ids) != len(set(shared_ids)):
        raise OracleError("shared constraint IDs must be complete and unique")

    scores: dict[str, dict[str, int]] = {}
    welfare: dict[str, int] = {}
    feasible_ids: list[str] = []
    shared_violations: dict[str, list[str]] = {}
    principal_violations: dict[str, dict[str, list[str]]] = {}
    for plan_id in sorted(plans):
        decision = plans[plan_id]["decision"]
        violated_shared = [
            _constraint_id(constraint, "shared constraint")
            for constraint in shared_raw
            if isinstance(constraint, Mapping)
            and not _constraint_satisfied(decision, constraint)
        ]
        violated_by_principal: dict[str, list[str]] = {}
        plan_scores: dict[str, int] = {}
        for principal_id in principal_ids:
            principal = principals[principal_id]
            hard_constraints = principal["hard_constraints"]
            violated_by_principal[principal_id] = [
                _constraint_id(constraint, f"{principal_id} hard constraint")
                for constraint in hard_constraints
                if isinstance(constraint, Mapping)
                and not _constraint_satisfied(decision, constraint)
            ]
            plan_scores[principal_id] = _soft_score(decision, principal)
        scores[plan_id] = plan_scores
        welfare[plan_id] = sum(
            weights[principal_id] * plan_scores[principal_id]
            for principal_id in principal_ids
        )
        shared_violations[plan_id] = sorted(violated_shared)
        principal_violations[plan_id] = {
            principal_id: sorted(values)
            for principal_id, values in violated_by_principal.items()
        }
        if not violated_shared and all(not values for values in violated_by_principal.values()):
            feasible_ids.append(plan_id)
    feasible_ids.sort()
    if len(feasible_ids) < 2:
        raise OracleError("task has fewer than two jointly feasible plans")

    ideal = {
        principal_id: max(scores[plan_id][principal_id] for plan_id in feasible_ids)
        for principal_id in principal_ids
    }
    regrets = {
        plan_id: {
            principal_id: max(0, ideal[principal_id] - scores[plan_id][principal_id])
            for principal_id in principal_ids
        }
        for plan_id in sorted(plans)
    }
    pareto_ids = _pareto_ids(feasible_ids, scores, principal_ids)
    distinct_vectors = {
        tuple(scores[plan_id][principal_id] for principal_id in principal_ids)
        for plan_id in feasible_ids
    }
    if len(distinct_vectors) < 2:
        raise OracleError("task has fewer than two distinct feasible score vectors")
    dominated_ids = sorted(set(feasible_ids) - set(pareto_ids))
    if not dominated_ids:
        raise OracleError("task has no dominated jointly feasible plan")

    def representative_key(plan_id: str) -> tuple[int, int, str]:
        return (
            -welfare[plan_id],
            max(regrets[plan_id].values()),
            canonical_json(plans[plan_id]),
        )

    representative = min(feasible_ids, key=representative_key)
    plan_evaluations: dict[str, dict[str, Any]] = {}
    for plan_id in sorted(plans):
        feasible = plan_id in set(feasible_ids)
        plan_evaluations[plan_id] = {
            "feasible": feasible,
            "utility_bp": scores[plan_id],
            "utilities": scores[plan_id],
            "regret_bp": regrets[plan_id],
            "worst_principal_regret_bp": max(regrets[plan_id].values()),
            "weighted_welfare": welfare[plan_id],
            "violated_shared_constraints": shared_violations[plan_id],
            "violated_hard_constraints": principal_violations[plan_id],
        }
    pareto_vectors = sorted(
        {
            tuple(scores[plan_id][principal_id] for principal_id in principal_ids)
            for plan_id in pareto_ids
        }
    )
    return {
        "oracle_schema_version": "coordcap-oracle-1.0.0",
        "principal_ids": principal_ids,
        "priority_weights": weights,
        "domain_plan_ids": sorted(plans),
        "feasible_plan_ids": feasible_ids,
        "joint_feasible_plan_ids": feasible_ids,
        "plan_evaluations": plan_evaluations,
        "utility_bp_by_plan": {plan_id: scores[plan_id] for plan_id in sorted(plans)},
        "ideal_bp": ideal,
        "ideal_utilities": ideal,
        "regret_bp_by_plan": regrets,
        "worst_principal_regret_bp_by_plan": {
            plan_id: max(regrets[plan_id].values()) for plan_id in sorted(plans)
        },
        "weighted_welfare": welfare,
        "weighted_welfare_by_plan": welfare,
        "pareto_plan_ids": pareto_ids,
        "pareto_utility_vectors": [list(vector) for vector in pareto_vectors],
        "dominated_feasible_plan_ids": dominated_ids,
        "representative_plan_id": representative,
        "gold_plan_id": representative,
        "representative_weighted_welfare": welfare[representative],
        "gold_weighted_welfare": welfare[representative],
    }


compute_oracle = solve_public_task
build_oracle = solve_public_task
solve_task = solve_public_task
