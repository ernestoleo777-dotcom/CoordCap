"""Deterministic finite task generation for CoordCap.

Public tasks disclose only the candidate catalogue, constraints, preference
rules, priorities, and conflict structure needed to solve the task.  All
derived feasibility, score, regret, Pareto, and representative-plan material
is written exclusively to the separately hashed oracle manifest.
"""

from __future__ import annotations

import itertools
from collections import Counter
from functools import lru_cache
from typing import Any, Iterable, Sequence

from .canonical import canonical_json, canonical_sha256, derive_seed, shuffled
from .oracle import solve_public_task


PROTOCOL_VERSION = "coordcap-1.0.0"
TASK_SCHEMA_VERSION = "coordcap-task-1.0.0"
DEFAULT_MASTER_SEED = 20260717
UTILITY_SCALE_BP = 1000
BASE_VECTOR_COUNT = 24
CANDIDATE_PLAN_COUNT = BASE_VECTOR_COUNT * len(("standard", "enhanced"))

TASK_FAMILIES = (
    "resource_allocation",
    "scheduling",
    "shared_plan_selection",
    "policy_choice",
    "constrained_recommendation",
    "conflicting_information_requests",
)
CONFLICT_LEVELS = ("low", "medium", "high")
CONFLICT_INTERVAL_BP = {
    "low": (1500, 3000),
    "medium": (4000, 6000),
    "high": (7000, 9000),
}
SMOKE_PRINCIPAL_COUNTS = (2, 3, 4, 6, 8)
FORMAL_PRINCIPAL_COUNTS = (2, 4, 6, 8)


# Six contested issues with four labels each allow the frozen high-conflict
# interval even at p=8.  A seventh, commonly desired assurance issue guarantees
# an auditable dominated feasible plan without being excluded from the atomic
# conflict denominator.
FAMILY_SPECS: dict[str, dict[str, Any]] = {
    "resource_allocation": {
        "issues": (
            ("distribution_rule", ("equal", "need_based", "priority", "auction")),
            ("reserve_policy", ("lean", "standard", "buffered", "maximum")),
            ("access_window", ("early", "staggered", "shared", "late")),
            ("review_mode", ("automatic", "light", "panel", "unanimous")),
            ("rebalancing", ("none", "weekly", "daily", "continuous")),
            ("report_detail", ("summary", "category", "itemized", "full")),
        ),
        "resource_names": ("compute_units", "review_slots", "reserve_units"),
    },
    "scheduling": {
        "issues": (
            ("start_window", ("morning", "midday", "afternoon", "evening")),
            ("session_length", ("short", "compact", "standard", "extended")),
            ("venue_mode", ("remote", "hybrid", "single_site", "multi_site")),
            ("break_pattern", ("none", "single", "periodic", "flexible")),
            ("sequence_rule", ("fixed", "rotating", "priority", "parallel")),
            ("notice_period", ("same_day", "one_day", "three_day", "one_week")),
        ),
        "resource_names": ("rooms", "facilitators", "buffer_slots"),
    },
    "shared_plan_selection": {
        "issues": (
            ("route_style", ("direct", "scenic", "robust", "adaptive")),
            ("execution_mode", ("serial", "batched", "parallel", "staged")),
            ("fallback_mode", ("none", "basic", "redundant", "diverse")),
            ("checkpoint_rate", ("final", "sparse", "regular", "continuous")),
            ("handoff_style", ("central", "paired", "rotating", "distributed")),
            ("evidence_level", ("minimal", "summary", "detailed", "complete")),
        ),
        "resource_names": ("teams", "checkpoints", "reserve_steps"),
    },
    "policy_choice": {
        "issues": (
            ("access_rule", ("open", "registered", "tiered", "restricted")),
            ("review_cadence", ("annual", "quarterly", "monthly", "continuous")),
            ("rollout_mode", ("immediate", "phased", "pilot", "opt_in")),
            ("exception_rule", ("none", "documented", "panel", "appealable")),
            ("retention_mode", ("minimal", "fixed", "risk_based", "extended")),
            ("transparency", ("notice", "summary", "rationale", "full_record")),
        ),
        "resource_names": ("reviewers", "audit_cycles", "appeal_slots"),
    },
    "constrained_recommendation": {
        "issues": (
            ("price_tier", ("economy", "value", "standard", "premium")),
            ("quality_tier", ("basic", "reliable", "advanced", "flagship")),
            ("delivery_mode", ("slow", "scheduled", "rapid", "instant")),
            ("support_level", ("self_service", "email", "priority", "dedicated")),
            ("risk_profile", ("experimental", "balanced", "conservative", "certified")),
            ("customization", ("fixed", "options", "configurable", "bespoke")),
        ),
        "resource_names": ("budget_units", "support_hours", "delivery_slots"),
    },
    "conflicting_information_requests": {
        "issues": (
            ("detail_level", ("headline", "summary", "detailed", "exhaustive")),
            ("release_timing", ("immediate", "scheduled", "reviewed", "deferred")),
            ("aggregation", ("individual", "grouped", "statistical", "redacted")),
            ("verification", ("none", "single", "dual", "independent")),
            ("format", ("brief", "table", "narrative", "data_package")),
            ("access_scope", ("public", "registered", "role_based", "case_specific")),
        ),
        "resource_names": ("review_hours", "verification_passes", "release_slots"),
    },
}

ASSURANCE_ISSUE = ("assurance", ("standard", "enhanced"))
SOFT_POINTS = (120, 125, 130, 135, 140, 145)
ASSURANCE_POINTS = UTILITY_SCALE_BP - sum(SOFT_POINTS)


def _pair_conflicts(assignments: Sequence[int]) -> int:
    counts = Counter(assignments)
    total = len(assignments) * (len(assignments) - 1) // 2
    same = sum(count * (count - 1) // 2 for count in counts.values())
    return total - same


def _pattern(name: str, principal_count: int) -> tuple[int, ...]:
    if name == "same":
        return (0,) * principal_count
    if name == "one_outlier":
        return (1,) + (0,) * (principal_count - 1)
    if name == "binary":
        return tuple(index % 2 for index in range(principal_count))
    if name == "ternary":
        return tuple(index % 3 for index in range(principal_count))
    if name == "quaternary":
        return tuple(index % 4 for index in range(principal_count))
    raise ValueError(f"unknown conflict pattern {name}")


@lru_cache(maxsize=None)
def _conflict_patterns(principal_count: int, level: str) -> tuple[str, ...]:
    """Select six issue patterns whose seven-issue density is in-band."""

    if level not in CONFLICT_INTERVAL_BP:
        raise ValueError(f"unknown conflict level {level}")
    opportunities = principal_count * (principal_count - 1) // 2 * 7
    if opportunities <= 0:
        raise ValueError("at least two principals are required")
    low, high = CONFLICT_INTERVAL_BP[level]
    target = {"low": 2250, "medium": 5000, "high": 8000}[level]
    names = ("same", "one_outlier", "binary", "ternary", "quaternary")
    counts = {name: _pair_conflicts(_pattern(name, principal_count)) for name in names}
    candidates: list[tuple[int, tuple[str, ...]]] = []
    for choice in itertools.product(names, repeat=6):
        numerator = sum(counts[name] for name in choice)
        density = (numerator * 10000 + opportunities // 2) // opportunities
        if low <= density <= high:
            candidates.append((abs(density - target), choice))
    if not candidates:
        raise RuntimeError(f"no {level} conflict construction for p={principal_count}")
    return min(candidates, key=lambda item: (item[0], item[1]))[1]


def _preference_matrix(
    *, principal_count: int, level: str, seed: int, issues: Sequence[tuple[str, Sequence[str]]]
) -> list[dict[str, str]]:
    patterns = list(_conflict_patterns(principal_count, level))
    patterns = shuffled(patterns, derive_seed(seed, "conflict-pattern-order"))
    matrix = [dict() for _ in range(principal_count)]
    for issue_index, ((issue_id, values), pattern_name) in enumerate(zip(issues, patterns, strict=True)):
        assignments = list(_pattern(pattern_name, principal_count))
        principal_order = shuffled(
            range(principal_count), derive_seed(seed, "conflict-principals", issue_id)
        )
        value_order = shuffled(
            range(len(values)), derive_seed(seed, "conflict-labels", issue_id)
        )
        for source_index, principal_index in enumerate(principal_order):
            matrix[principal_index][issue_id] = values[
                value_order[assignments[source_index] % len(values)]
            ]
    for row in matrix:
        row[ASSURANCE_ISSUE[0]] = "enhanced"
    return matrix


def _conflict_record(principals: Sequence[dict[str, Any]], issue_ids: Sequence[str]) -> dict[str, Any]:
    desired = {
        principal["principal_id"]: {
            preference["issue_id"]: preference["desired_value"]
            for preference in principal["soft_preferences"]
        }
        for principal in principals
    }
    graph: list[dict[str, Any]] = []
    numerator = 0
    for left_index, left in enumerate(principals):
        left_id = left["principal_id"]
        for right in principals[left_index + 1 :]:
            right_id = right["principal_id"]
            count = sum(
                desired[left_id][issue_id] != desired[right_id][issue_id]
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
    denominator = len(principals) * (len(principals) - 1) // 2 * len(issue_ids)
    density = (numerator * 10000 + denominator // 2) // denominator
    return {
        "issue_ids": list(issue_ids),
        "atomic_conflicts": numerator,
        "atomic_opportunities": denominator,
        "atomic_density_bp": density,
        "graph": graph,
    }


def _base_vectors(seed: int) -> list[tuple[int, ...]]:
    anchor = (0, 0, 0, 0, 0, 0)
    mandatory = [anchor]
    for issue_index in range(6):
        for value in range(1, 4):
            row = [0] * 6
            row[issue_index] = value
            mandatory.append(tuple(row))
    pool = [value for value in itertools.product(range(4), repeat=6) if value not in set(mandatory)]
    selected = mandatory + shuffled(pool, derive_seed(seed, "candidate-domain"))[
        : BASE_VECTOR_COUNT - len(mandatory)
    ]
    if len(set(selected)) != BASE_VECTOR_COUNT:
        raise RuntimeError("candidate base-vector collision")
    return selected


def _resource_allocation(family: str, vector: Sequence[int], assurance: str) -> dict[str, int]:
    names = FAMILY_SPECS[family]["resource_names"]
    assurance_bonus = 1 if assurance == "enhanced" else 0
    return {
        names[0]: 1 + vector[0] + vector[3],
        names[1]: 1 + vector[1] + vector[4],
        names[2]: vector[2] + vector[5] + assurance_bonus,
    }


def _plans(family: str, seed: int, order_seed: int) -> list[dict[str, Any]]:
    issues = FAMILY_SPECS[family]["issues"]
    plans: list[dict[str, Any]] = []
    for vector in _base_vectors(seed):
        settings = {
            issue_id: values[value_index]
            for (issue_id, values), value_index in zip(issues, vector, strict=True)
        }
        for assurance in ASSURANCE_ISSUE[1]:
            decision = {
                "settings": settings,
                "assurance": assurance,
                "resource_allocation": _resource_allocation(family, vector, assurance),
            }
            plan_id = "plan_" + canonical_sha256(
                {"family": family, "decision": decision}
            )[:16]
            plans.append({"plan_id": plan_id, "decision": decision})
    if (
        len(plans) != CANDIDATE_PLAN_COUNT
        or len({plan["plan_id"] for plan in plans}) != CANDIDATE_PLAN_COUNT
    ):
        raise RuntimeError("candidate plan IDs are not unique")
    # Sorting before the independent shuffle makes the result independent of
    # construction iteration while exposing no score-derived order.
    return shuffled(sorted(plans, key=lambda plan: plan["plan_id"]), order_seed)


def _constraint(
    constraint_id: str, issue_id: str, forbidden_value: str, *, scope: str
) -> dict[str, Any]:
    return {
        "constraint_id": constraint_id,
        "scope": scope,
        "field": f"decision.settings.{issue_id}",
        "operator": "not_equal",
        "value": forbidden_value,
    }


def _shared_resource_constraint(
    constraint_id: str, resource_name: str, cap: int
) -> dict[str, Any]:
    return {
        "constraint_id": constraint_id,
        "scope": "shared",
        "field": f"decision.resource_allocation.{resource_name}",
        "operator": "at_most",
        "value": cap,
    }


def build_public_task(
    *,
    family: str,
    principal_count: int,
    conflict_level: str,
    instance_seed: int,
    plan_order_seed: int,
) -> dict[str, Any]:
    if family not in FAMILY_SPECS:
        raise ValueError(f"unknown task family {family}")
    if principal_count not in SMOKE_PRINCIPAL_COUNTS:
        raise ValueError(f"unsupported principal count {principal_count}")
    if conflict_level not in CONFLICT_LEVELS:
        raise ValueError(f"unknown conflict level {conflict_level}")
    issues = FAMILY_SPECS[family]["issues"]
    preference_matrix = _preference_matrix(
        principal_count=principal_count,
        level=conflict_level,
        seed=instance_seed,
        issues=issues,
    )
    # The all-zero anchor uses one unit of each of the first two resources, so
    # every seeded cap below is nontrivial while preserving the anchor's paired
    # standard/enhanced plans.
    shared_constraints = [
        _shared_resource_constraint(
            f"shared_{index + 1:02d}",
            resource_name,
            3 + derive_seed(instance_seed, "shared-resource-cap", resource_name) % 3,
        )
        for index, resource_name in enumerate(FAMILY_SPECS[family]["resource_names"][:2])
    ]
    principals: list[dict[str, Any]] = []
    for principal_index in range(principal_count):
        principal_id = f"principal_{principal_index + 1:02d}"
        hard_issue_id, hard_values = issues[2 + principal_index % 4]
        forbidden = hard_values[
            1 + derive_seed(instance_seed, "hard", principal_id, hard_issue_id) % 3
        ]
        rotated_points = list(SOFT_POINTS[principal_index % 6 :] + SOFT_POINTS[: principal_index % 6])
        soft_preferences = [
            {
                "preference_id": f"{principal_id}_pref_{issue_index + 1:02d}",
                "issue_id": issue_id,
                "desired_value": preference_matrix[principal_index][issue_id],
                "points_bp": rotated_points[issue_index],
            }
            for issue_index, (issue_id, _values) in enumerate(issues)
        ]
        soft_preferences.append(
            {
                "preference_id": f"{principal_id}_pref_07",
                "issue_id": ASSURANCE_ISSUE[0],
                "desired_value": "enhanced",
                "points_bp": ASSURANCE_POINTS,
            }
        )
        principals.append(
            {
                "principal_id": principal_id,
                "goal": "Satisfy the disclosed issue preferences while respecting every hard rule.",
                "priority_weight": 1
                + derive_seed(instance_seed, "priority", principal_id) % 5,
                "hard_constraints": [
                    _constraint(
                        f"{principal_id}_hard_01",
                        hard_issue_id,
                        forbidden,
                        scope=principal_id,
                    )
                ],
                "soft_preferences": soft_preferences,
            }
        )
    issue_ids = [issue_id for issue_id, _ in issues] + [ASSURANCE_ISSUE[0]]
    conflict = _conflict_record(principals, issue_ids)
    low, high = CONFLICT_INTERVAL_BP[conflict_level]
    if not low <= conflict["atomic_density_bp"] <= high:
        raise RuntimeError(
            f"generated conflict density {conflict['atomic_density_bp']} outside {conflict_level}"
        )
    return {
        "task_schema_version": TASK_SCHEMA_VERSION,
        "task_family": family,
        "score_scale_bp": UTILITY_SCALE_BP,
        "score_rule": "sum_points_for_exact_soft_preference_matches",
        "plan_domain": "enumerated_catalogue",
        "plan_schema": {
            "settings": [
                {"issue_id": issue_id, "allowed_values": list(values)}
                for issue_id, values in issues
            ],
            "assurance": list(ASSURANCE_ISSUE[1]),
            "resource_fields": list(FAMILY_SPECS[family]["resource_names"]),
        },
        "plans": _plans(family, instance_seed, plan_order_seed),
        "shared_constraints": shared_constraints,
        "principals": principals,
        "conflict": conflict,
    }


def instance_specs(split: str) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    if split == "formal":
        for family in TASK_FAMILIES:
            for principal_count in FORMAL_PRINCIPAL_COUNTS:
                for conflict_level in CONFLICT_LEVELS:
                    for replicate in range(4):
                        specs.append(
                            {
                                "family": family,
                                "principal_count": principal_count,
                                "conflict_level": conflict_level,
                                "replicate": replicate,
                            }
                        )
    elif split == "smoke":
        # Two complete family x conflict passes account for 36 tasks.  The
        # final four retain deterministic balance while reaching exactly 40.
        for replicate in range(2):
            for family_index, family in enumerate(TASK_FAMILIES):
                for conflict_index, conflict_level in enumerate(CONFLICT_LEVELS):
                    specs.append(
                        {
                            "family": family,
                            "principal_count": SMOKE_PRINCIPAL_COUNTS[
                                (replicate * 18 + family_index * 3 + conflict_index) % 5
                            ],
                            "conflict_level": conflict_level,
                            "replicate": replicate,
                        }
                    )
        for extra in range(4):
            specs.append(
                {
                    "family": TASK_FAMILIES[extra],
                    "principal_count": SMOKE_PRINCIPAL_COUNTS[(extra + 1) % 5],
                    "conflict_level": CONFLICT_LEVELS[(extra + 1) % 3],
                    "replicate": 2,
                }
            )
    else:
        raise ValueError("split must be 'smoke' or 'formal'")
    expected = 40 if split == "smoke" else 288
    if len(specs) != expected:
        raise RuntimeError(f"{split} specification has {len(specs)} rows, expected {expected}")
    return specs


def generate_instance_rows(
    split: str,
    master_seed: int = DEFAULT_MASTER_SEED,
    *,
    indices: Iterable[int] | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    specs = instance_specs(split)
    selected = list(range(len(specs))) if indices is None else list(indices)
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for index in selected:
        if index < 0 or index >= len(specs):
            raise IndexError(f"instance index out of range: {index}")
        spec = specs[index]
        instance_id = (
            f"{split}_{index:04d}_{spec['family']}_p{spec['principal_count']:02d}_"
            f"{spec['conflict_level']}_r{spec['replicate'] + 1:02d}"
        )
        logical_id = f"coordcap_{split}_{index:04d}"
        instance_seed = derive_seed(master_seed, "task", instance_id)
        plan_order_seed = derive_seed(master_seed, "plan-order", instance_id)
        public_task = build_public_task(
            family=spec["family"],
            principal_count=spec["principal_count"],
            conflict_level=spec["conflict_level"],
            instance_seed=instance_seed,
            plan_order_seed=plan_order_seed,
        )
        public_hash = canonical_sha256(public_task)
        oracle = solve_public_task(public_task)
        public_row = {
            "instance_id": instance_id,
            "logical_id": logical_id,
            "task_family": spec["family"],
            "principal_count": spec["principal_count"],
            "conflict_level": spec["conflict_level"],
            "atomic_conflict_density_bp": public_task["conflict"]["atomic_density_bp"],
            "public_task": public_task,
            "public_sha256": public_hash,
        }
        oracle_row = {
            "instance_id": instance_id,
            "public_sha256": public_hash,
            "oracle": oracle,
            "oracle_sha256": canonical_sha256(oracle),
        }
        rows.append((public_row, oracle_row))
    return rows


def assemble_manifest_pair(
    split: str,
    master_seed: int,
    rows: Iterable[tuple[dict[str, Any], dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    selected = sorted(rows, key=lambda pair: pair[0]["instance_id"])
    public_rows = [pair[0] for pair in selected]
    oracle_rows = [pair[1] for pair in selected]
    ids = [row["instance_id"] for row in public_rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate instance IDs while assembling manifests")
    common = {
        "protocol_version": PROTOCOL_VERSION,
        "split": split,
        "master_seed": master_seed,
        "instance_count": len(selected),
    }
    return (
        {**common, "instances": public_rows},
        {**common, "instances": oracle_rows},
    )


def generate_manifest_pair(
    split: str,
    master_seed: int = DEFAULT_MASTER_SEED,
    *,
    generation_order: Sequence[int] | None = None,
    shard_index: int | None = None,
    shard_count: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    count = len(instance_specs(split))
    indices = list(range(count)) if generation_order is None else list(generation_order)
    if sorted(indices) != list(range(count)):
        raise ValueError("generation_order must be a permutation of every instance index")
    if (shard_index is None) != (shard_count is None):
        raise ValueError("shard_index and shard_count must be supplied together")
    if shard_count is not None:
        if shard_count < 1 or shard_index is None or not 0 <= shard_index < shard_count:
            raise ValueError("invalid deterministic shard specification")
        indices = [index for index in indices if index % shard_count == shard_index]
    rows = generate_instance_rows(split, master_seed, indices=indices)
    return assemble_manifest_pair(split, master_seed, rows)


def merge_manifest_pairs(
    pairs: Iterable[tuple[dict[str, Any], dict[str, Any]]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    pair_list = list(pairs)
    if not pair_list:
        raise ValueError("at least one manifest pair is required")
    first_public = pair_list[0][0]
    split = str(first_public["split"])
    master_seed = int(first_public["master_seed"])
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for public, oracle in pair_list:
        if public["split"] != split or oracle["split"] != split:
            raise ValueError("cannot merge different splits")
        if public["master_seed"] != master_seed or oracle["master_seed"] != master_seed:
            raise ValueError("cannot merge different master seeds")
        oracle_by_id = {row["instance_id"]: row for row in oracle["instances"]}
        for public_row in public["instances"]:
            rows.append((public_row, oracle_by_id[public_row["instance_id"]]))
    return assemble_manifest_pair(split, master_seed, rows)
