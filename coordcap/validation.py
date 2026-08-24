"""Independent solver and fail-closed validation for CoordCap manifests."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from .canonical import canonical_json, canonical_sha256


UTILITY_SCALE_BP = 1000
PROTOCOL_VERSION = "coordcap-1.0.0"
CONFLICT_INTERVAL_BP = {
    "low": (1500, 3000),
    "medium": (4000, 6000),
    "high": (7000, 9000),
}
FORBIDDEN_PUBLIC_TOKENS = (
    "gold",
    "utility",
    "regret",
    "pareto",
    "answer",
    "optimal",
    "representative",
    "welfare",
    "feasible",
)


class ValidationError(ValueError):
    """Raised when any frozen generation or separation invariant fails."""


def _walk(value: Any, path: str = "$") -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}"
            rows.append((child, key))
            rows.extend(_walk(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            rows.extend(_walk(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        rows.append((path, value))
    return rows


def scan_public_leakage(public_task: Mapping[str, Any]) -> list[str]:
    """Return key/value locations containing oracle-only vocabulary."""

    findings: list[str] = []
    for path, value in _walk(public_task):
        lowered = str(value).lower()
        if any(token in lowered for token in FORBIDDEN_PUBLIC_TOKENS):
            findings.append(path)
    return sorted(set(findings))


def assert_public_safe(public_task: Mapping[str, Any]) -> None:
    findings = scan_public_leakage(public_task)
    if findings:
        raise ValidationError(f"public/oracle leakage canary fired at {findings[:8]}")


def _read(decision: Mapping[str, Any], path: str) -> Any:
    cursor: Any = decision
    components = path.split(".")
    if components and components[0] == "decision":
        components = components[1:]
    for component in components:
        if not isinstance(cursor, Mapping) or component not in cursor:
            raise ValidationError(f"missing decision field {path}")
        cursor = cursor[component]
    return cursor


def _passes(decision: Mapping[str, Any], rule: Mapping[str, Any]) -> bool:
    observed = _read(decision, str(rule["field"]))
    operator = rule["operator"]
    if operator == "equal":
        return observed == rule.get("value")
    if operator == "not_equal":
        return observed != rule.get("value")
    if operator == "one_of":
        return observed in rule.get("values", [])
    if operator == "not_in":
        return observed not in rule.get("values", [])
    if operator == "at_least":
        return isinstance(observed, int) and observed >= rule.get("value")
    if operator == "at_most":
        return isinstance(observed, int) and observed <= rule.get("value")
    raise ValidationError(f"unsupported operator {operator}")


def independent_solve_public_task(public_task: Mapping[str, Any]) -> dict[str, Any]:
    """Second exhaustive implementation; it does not call the reference solver."""

    plan_rows = public_task.get("plans")
    principal_rows = public_task.get("principals")
    shared_rules = public_task.get("shared_constraints")
    if not isinstance(plan_rows, list) or not isinstance(principal_rows, list):
        raise ValidationError("plans and principals must be arrays")
    if not isinstance(shared_rules, list):
        raise ValidationError("shared_constraints must be an array")
    plans: dict[str, dict[str, Any]] = {}
    for row in plan_rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("plan_id"), str):
            raise ValidationError("malformed candidate plan")
        plan_id = row["plan_id"]
        if plan_id in plans or not isinstance(row.get("decision"), Mapping):
            raise ValidationError("duplicate or malformed candidate plan")
        plans[plan_id] = dict(row)
    principals: dict[str, dict[str, Any]] = {}
    priorities: dict[str, int] = {}
    for row in principal_rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("principal_id"), str):
            raise ValidationError("malformed principal")
        principal_id = row["principal_id"]
        priority = row.get("priority_weight")
        if principal_id in principals or isinstance(priority, bool) or not isinstance(priority, int) or priority < 1:
            raise ValidationError("duplicate principal or invalid priority")
        principals[principal_id] = dict(row)
        priorities[principal_id] = priority
    principal_ids = sorted(principals)
    values: dict[str, dict[str, int]] = {}
    weighted: dict[str, int] = {}
    shared_failures: dict[str, list[str]] = {}
    hard_failures: dict[str, dict[str, list[str]]] = {}
    feasible: list[str] = []
    for plan_id in sorted(plans):
        decision = plans[plan_id]["decision"]
        shared_failed = sorted(
            str(rule["constraint_id"])
            for rule in shared_rules
            if isinstance(rule, Mapping) and not _passes(decision, rule)
        )
        per_principal: dict[str, list[str]] = {}
        score_row: dict[str, int] = {}
        for principal_id in principal_ids:
            principal = principals[principal_id]
            hard = principal.get("hard_constraints")
            soft = principal.get("soft_preferences")
            if not isinstance(hard, list) or not isinstance(soft, list):
                raise ValidationError("principal constraints/preferences must be arrays")
            per_principal[principal_id] = sorted(
                str(rule["constraint_id"])
                for rule in hard
                if isinstance(rule, Mapping) and not _passes(decision, rule)
            )
            available = 0
            earned = 0
            for preference in soft:
                if not isinstance(preference, Mapping):
                    raise ValidationError("soft preference must be an object")
                points = preference.get("points_bp")
                issue = preference.get("issue_id")
                if isinstance(points, bool) or not isinstance(points, int) or not isinstance(issue, str):
                    raise ValidationError("invalid soft preference")
                available += points
                observed = (
                    decision.get("assurance")
                    if issue == "assurance"
                    else _read(decision, f"settings.{issue}")
                )
                if observed == preference.get("desired_value"):
                    earned += points
            if available != UTILITY_SCALE_BP:
                raise ValidationError("soft preference points must total 1000")
            score_row[principal_id] = earned
        values[plan_id] = score_row
        weighted[plan_id] = sum(
            priorities[principal_id] * score_row[principal_id]
            for principal_id in principal_ids
        )
        shared_failures[plan_id] = shared_failed
        hard_failures[plan_id] = per_principal
        if not shared_failed and not any(per_principal.values()):
            feasible.append(plan_id)
    feasible.sort()
    if len(feasible) < 2:
        raise ValidationError("fewer than two jointly feasible plans")
    ideals = {
        principal_id: max(values[plan_id][principal_id] for plan_id in feasible)
        for principal_id in principal_ids
    }
    regrets = {
        plan_id: {
            principal_id: max(0, ideals[principal_id] - values[plan_id][principal_id])
            for principal_id in principal_ids
        }
        for plan_id in sorted(plans)
    }
    frontier: list[str] = []
    for candidate in feasible:
        is_dominated = False
        for challenger in feasible:
            if candidate == challenger:
                continue
            if all(
                values[challenger][pid] >= values[candidate][pid]
                for pid in principal_ids
            ) and any(
                values[challenger][pid] > values[candidate][pid]
                for pid in principal_ids
            ):
                is_dominated = True
                break
        if not is_dominated:
            frontier.append(candidate)
    frontier.sort()
    dominated = sorted(set(feasible) - set(frontier))
    if not dominated:
        raise ValidationError("no dominated feasible plan")
    if len(
        {
            tuple(values[plan_id][principal_id] for principal_id in principal_ids)
            for plan_id in feasible
        }
    ) < 2:
        raise ValidationError("fewer than two feasible score vectors")
    chosen = min(
        feasible,
        key=lambda plan_id: (
            -weighted[plan_id],
            max(regrets[plan_id].values()),
            canonical_json(plans[plan_id]),
        ),
    )
    evaluations = {
        plan_id: {
            "feasible": plan_id in set(feasible),
            "utility_bp": values[plan_id],
            "utilities": values[plan_id],
            "regret_bp": regrets[plan_id],
            "worst_principal_regret_bp": max(regrets[plan_id].values()),
            "weighted_welfare": weighted[plan_id],
            "violated_shared_constraints": shared_failures[plan_id],
            "violated_hard_constraints": hard_failures[plan_id],
        }
        for plan_id in sorted(plans)
    }
    frontier_vectors = sorted(
        {
            tuple(values[plan_id][principal_id] for principal_id in principal_ids)
            for plan_id in frontier
        }
    )
    return {
        "oracle_schema_version": "coordcap-oracle-1.0.0",
        "principal_ids": principal_ids,
        "priority_weights": priorities,
        "domain_plan_ids": sorted(plans),
        "feasible_plan_ids": feasible,
        "joint_feasible_plan_ids": feasible,
        "plan_evaluations": evaluations,
        "utility_bp_by_plan": {plan_id: values[plan_id] for plan_id in sorted(plans)},
        "ideal_bp": ideals,
        "ideal_utilities": ideals,
        "regret_bp_by_plan": regrets,
        "worst_principal_regret_bp_by_plan": {
            plan_id: max(regrets[plan_id].values()) for plan_id in sorted(plans)
        },
        "weighted_welfare": weighted,
        "weighted_welfare_by_plan": weighted,
        "pareto_plan_ids": frontier,
        "pareto_utility_vectors": [list(vector) for vector in frontier_vectors],
        "dominated_feasible_plan_ids": dominated,
        "representative_plan_id": chosen,
        "gold_plan_id": chosen,
        "representative_weighted_welfare": weighted[chosen],
        "gold_weighted_welfare": weighted[chosen],
    }


def _conflict_from_principals(public_task: Mapping[str, Any]) -> dict[str, Any]:
    conflict = public_task.get("conflict")
    principals = public_task.get("principals")
    if not isinstance(conflict, Mapping) or not isinstance(principals, list):
        raise ValidationError("missing public conflict/principal records")
    issue_ids = conflict.get("issue_ids")
    if not isinstance(issue_ids, list) or not issue_ids:
        raise ValidationError("conflict issue_ids must be non-empty")
    desired: dict[str, dict[str, Any]] = {}
    for principal in principals:
        if not isinstance(principal, Mapping) or not isinstance(principal.get("principal_id"), str):
            raise ValidationError("malformed principal in conflict audit")
        preferences = principal.get("soft_preferences")
        if not isinstance(preferences, list):
            raise ValidationError("missing soft preferences")
        desired[principal["principal_id"]] = {
            preference["issue_id"]: preference["desired_value"]
            for preference in preferences
            if isinstance(preference, Mapping)
        }
    graph: list[dict[str, Any]] = []
    numerator = 0
    principal_ids = [principal["principal_id"] for principal in principals]
    for left_index, left_id in enumerate(principal_ids):
        for right_id in principal_ids[left_index + 1 :]:
            count = sum(
                desired[left_id].get(issue_id) != desired[right_id].get(issue_id)
                for issue_id in issue_ids
            )
            numerator += count
            if count:
                graph.append(
                    {
                        "left_principal_id": left_id,
                        "right_principal_id": right_id,
                        "atomic_conflicts": count,
                        "rate_bp": (count * 10000 + len(issue_ids) // 2) // len(issue_ids),
                    }
                )
    denominator = len(principal_ids) * (len(principal_ids) - 1) // 2 * len(issue_ids)
    return {
        "issue_ids": issue_ids,
        "atomic_conflicts": numerator,
        "atomic_opportunities": denominator,
        "atomic_density_bp": (numerator * 10000 + denominator // 2) // denominator,
        "graph": graph,
    }


def _dangerous_candidate_orders(public_task: Mapping[str, Any], oracle: Mapping[str, Any]) -> list[list[str]]:
    plan_ids = list(oracle["domain_plan_ids"])
    feasible = set(oracle["feasible_plan_ids"])
    pareto = set(oracle["pareto_plan_ids"])
    welfare = oracle["weighted_welfare_by_plan"]
    regrets = oracle["regret_bp_by_plan"]
    return [
        sorted(plan_ids),
        sorted(plan_ids, key=lambda plan_id: (-welfare[plan_id], plan_id)),
        sorted(plan_ids, key=lambda plan_id: (0 if plan_id in feasible else 1, plan_id)),
        sorted(plan_ids, key=lambda plan_id: (0 if plan_id in pareto else 1, plan_id)),
        sorted(plan_ids, key=lambda plan_id: (max(regrets[plan_id].values()), plan_id)),
    ]


def validate_manifest_pair(
    public_manifest: Mapping[str, Any], oracle_manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate schemas, hashes, separation, conflicts, and both solvers."""

    for label, manifest in (("public", public_manifest), ("oracle", oracle_manifest)):
        if manifest.get("protocol_version") != PROTOCOL_VERSION:
            raise ValidationError(f"{label} manifest protocol_version mismatch")
        rows = manifest.get("instances")
        if not isinstance(rows, list) or manifest.get("instance_count") != len(rows):
            raise ValidationError(f"{label} manifest instance_count mismatch")
    if public_manifest.get("split") != oracle_manifest.get("split"):
        raise ValidationError("public/oracle split mismatch")
    if public_manifest.get("master_seed") != oracle_manifest.get("master_seed"):
        raise ValidationError("public/oracle seed mismatch")
    public_rows = public_manifest["instances"]
    oracle_rows = oracle_manifest["instances"]
    if len(public_rows) != len(oracle_rows):
        raise ValidationError("public/oracle instance count differs")
    densities: Counter[str] = Counter()
    for index, (public_row, oracle_row) in enumerate(zip(public_rows, oracle_rows, strict=True)):
        required_public = {
            "instance_id",
            "logical_id",
            "task_family",
            "principal_count",
            "conflict_level",
            "atomic_conflict_density_bp",
            "public_task",
            "public_sha256",
        }
        if set(public_row) != required_public:
            raise ValidationError(f"public instance {index} has unexpected fields")
        if set(oracle_row) != {"instance_id", "public_sha256", "oracle", "oracle_sha256"}:
            raise ValidationError(f"oracle instance {index} has unexpected fields")
        instance_id = public_row["instance_id"]
        if oracle_row["instance_id"] != instance_id:
            raise ValidationError("public/oracle instance order mismatch")
        public_task = public_row["public_task"]
        if canonical_sha256(public_task) != public_row["public_sha256"]:
            raise ValidationError(f"{instance_id}: public hash mismatch")
        if oracle_row["public_sha256"] != public_row["public_sha256"]:
            raise ValidationError(f"{instance_id}: oracle binding mismatch")
        if canonical_sha256(oracle_row["oracle"]) != oracle_row["oracle_sha256"]:
            raise ValidationError(f"{instance_id}: oracle hash mismatch")
        assert_public_safe(public_task)
        recomputed_conflict = _conflict_from_principals(public_task)
        if recomputed_conflict != public_task.get("conflict"):
            raise ValidationError(f"{instance_id}: conflict graph/density mismatch")
        density = recomputed_conflict["atomic_density_bp"]
        if density != public_row["atomic_conflict_density_bp"]:
            raise ValidationError(f"{instance_id}: outer conflict density mismatch")
        level = public_row["conflict_level"]
        if level not in CONFLICT_INTERVAL_BP or not (
            CONFLICT_INTERVAL_BP[level][0] <= density <= CONFLICT_INTERVAL_BP[level][1]
        ):
            raise ValidationError(f"{instance_id}: conflict level interval failure")
        densities[level] += 1
        independent = independent_solve_public_task(public_task)
        if independent != oracle_row["oracle"]:
            raise ValidationError(f"{instance_id}: independent solver disagreement")
        observed_order = [plan["plan_id"] for plan in public_task["plans"]]
        if any(observed_order == order for order in _dangerous_candidate_orders(public_task, independent)):
            raise ValidationError(f"{instance_id}: candidate order has an oracle-derived ordering")
    ids = [row["instance_id"] for row in public_rows]
    if len(ids) != len(set(ids)):
        raise ValidationError("duplicate public instance IDs")
    return {
        "status": "pass",
        "protocol_version": PROTOCOL_VERSION,
        "split": public_manifest["split"],
        "instance_count": len(public_rows),
        "public_only_recomputations": len(public_rows),
        "solver_agreements": len(public_rows),
        "leakage_findings": 0,
        "conflict_level_counts": dict(sorted(densities.items())),
    }
