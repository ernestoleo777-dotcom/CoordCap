"""Deterministic statistics for the frozen CoordCap protocol.

The functions in this module are deliberately independent of model records and
filesystem layout.  They operate on scored expected-run rows, preserve failed
rows, and derive every bootstrap random stream from the frozen seed plus a
stable namespace.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


BOOTSTRAP_SAMPLES = 2000
BOOTSTRAP_SEED = 20260717
PRIMARY_PRINCIPAL_COUNTS = (2, 4, 6, 8)

CAPACITY_JSR_MIN = 0.80
CAPACITY_HCVR_MAX = 0.05
CAPACITY_WORST_REGRET_MAX = 0.25

CAPACITY_GROUP_FIELDS = (
    "model",
    "method",
    "max_answer_tokens",
    "max_model_calls",
    "conflict_level",
)


class StatisticsError(ValueError):
    """Raised when scored rows cannot support the requested statistic."""


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def arithmetic_mean(values: Iterable[int | float]) -> float | None:
    numbers = [float(value) for value in values]
    return sum(numbers) / len(numbers) if numbers else None


def _percentile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _namespace_seed(seed: int, namespace: str) -> int:
    digest = hashlib.sha256(f"coordcap-bootstrap\0{seed}\0{namespace}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _clustered_strata(rows: Sequence[Mapping[str, Any]]) -> list[list[list[Mapping[str, Any]]]]:
    """Return task-family strata containing instance-level row clusters.

    The protocol requires all rows for a sampled instance to stay together.
    Most primary cells contain one row per instance, while this representation
    also behaves correctly for future paired or repeated rows.
    """

    by_family: dict[str, dict[str, list[Mapping[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        family = str(row.get("task_family") or "unknown")
        # The frozen analysis samples public instances.  ``logical_id`` is
        # retained for pairing order variants, but it must not accidentally
        # merge distinct public instances during the primary bootstrap.
        instance = str(row.get("instance_id") or row.get("logical_id") or row.get("run_id"))
        by_family[family][instance].append(row)
    return [
        [clusters[key] for key in sorted(clusters)]
        for _, clusters in sorted(by_family.items())
        if clusters
    ]


def _resample_rows(
    strata: Sequence[Sequence[Sequence[Mapping[str, Any]]]], rng: random.Random
) -> list[Mapping[str, Any]]:
    sampled: list[Mapping[str, Any]] = []
    for clusters in strata:
        for _ in range(len(clusters)):
            sampled.extend(clusters[rng.randrange(len(clusters))])
    return sampled


def _metric_mean(rows: Sequence[Mapping[str, Any]], field: str) -> float | None:
    values: list[float] = []
    for row in rows:
        number = _finite_number(row.get(field))
        if number is not None:
            values.append(number)
    return arithmetic_mean(values)


def family_stratified_bootstrap(
    rows: Sequence[Mapping[str, Any]],
    fields: Sequence[str],
    *,
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = BOOTSTRAP_SEED,
    namespace: str = "metrics",
) -> dict[str, dict[str, float | int | None]]:
    """Compute fixed-seed percentile intervals for row means.

    Rows are sampled as instance clusters within task-family strata.  Missing
    values are not silently converted to zero; a field with no numeric values
    is reported as unavailable.
    """

    if isinstance(samples, bool) or not isinstance(samples, int) or samples < 1:
        raise StatisticsError("bootstrap samples must be a positive integer")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise StatisticsError("bootstrap seed must be an integer")
    strata = _clustered_strata(rows)
    rng = random.Random(_namespace_seed(seed, namespace))
    draws: dict[str, list[float]] = {field: [] for field in fields}
    if strata:
        for _ in range(samples):
            sampled = _resample_rows(strata, rng)
            for field in fields:
                value = _metric_mean(sampled, field)
                if value is not None:
                    draws[field].append(value)
    result: dict[str, dict[str, float | int | None]] = {}
    for field in fields:
        point = _metric_mean(rows, field)
        values = draws[field]
        result[field] = {
            "point": point,
            "lower_95": _percentile(values, 0.025),
            "upper_95": _percentile(values, 0.975),
            "n_rows": len(rows),
            "bootstrap_samples": samples if point is not None and strata else 0,
        }
    return result


def _gate_diagnostics(
    estimates: Mapping[str, Mapping[str, float | int | None]],
) -> dict[str, bool]:
    jsr = estimates["joint_success"]
    hcvr = estimates["hard_constraint_violation"]
    regret = estimates["worst_principal_regret"]

    def value(metric: Mapping[str, Any], key: str) -> float | None:
        return _finite_number(metric.get(key))

    jsr_point = value(jsr, "point")
    jsr_lower = value(jsr, "lower_95")
    hcvr_point = value(hcvr, "point")
    hcvr_upper = value(hcvr, "upper_95")
    regret_point = value(regret, "point")
    regret_upper = value(regret, "upper_95")
    return {
        "jsr_point": jsr_point is not None and jsr_point >= CAPACITY_JSR_MIN,
        "jsr_lower_95": jsr_lower is not None and jsr_lower >= CAPACITY_JSR_MIN,
        "hcvr_point": hcvr_point is not None and hcvr_point <= CAPACITY_HCVR_MAX,
        "hcvr_upper_95": hcvr_upper is not None and hcvr_upper <= CAPACITY_HCVR_MAX,
        "worst_regret_point": (
            regret_point is not None and regret_point <= CAPACITY_WORST_REGRET_MAX
        ),
        "worst_regret_upper_95": (
            regret_upper is not None and regret_upper <= CAPACITY_WORST_REGRET_MAX
        ),
    }


def _joint_gate_pass_rate(
    rows: Sequence[Mapping[str, Any]],
    *,
    samples: int,
    seed: int,
    namespace: str,
) -> float | None:
    """Diagnostic only; it is not an additional capacity gate."""

    strata = _clustered_strata(rows)
    if not strata:
        return None
    rng = random.Random(_namespace_seed(seed, namespace + "/joint-gate"))
    passed = 0
    for _ in range(samples):
        sampled = _resample_rows(strata, rng)
        jsr = _metric_mean(sampled, "joint_success")
        hcvr = _metric_mean(sampled, "hard_constraint_violation")
        regret = _metric_mean(sampled, "worst_principal_regret")
        passed += int(
            jsr is not None
            and hcvr is not None
            and regret is not None
            and jsr >= CAPACITY_JSR_MIN
            and hcvr <= CAPACITY_HCVR_MAX
            and regret <= CAPACITY_WORST_REGRET_MAX
        )
    return passed / samples


def capacity_analysis(
    rows: Sequence[Mapping[str, Any]],
    *,
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = BOOTSTRAP_SEED,
    principal_counts: Sequence[int] = PRIMARY_PRINCIPAL_COUNTS,
) -> list[dict[str, Any]]:
    """Compute frozen capacity with point-and-bound gates and prefix monotonicity."""

    canonical_rows = []
    for row in rows:
        principal_count = row.get("principal_count")
        if (
            str(row.get("order_variant", "canonical")) == "canonical"
            and isinstance(principal_count, int)
            and not isinstance(principal_count, bool)
            and principal_count in principal_counts
        ):
            canonical_rows.append(row)
    grouped: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for row in canonical_rows:
        grouped[tuple(row.get(field) for field in CAPACITY_GROUP_FIELDS)].append(row)

    reports: list[dict[str, Any]] = []
    for key in sorted(grouped, key=lambda item: tuple(str(value) for value in item)):
        group_rows = grouped[key]
        identity = dict(zip(CAPACITY_GROUP_FIELDS, key, strict=True))
        per_count: list[dict[str, Any]] = []
        prefix_pass = True
        capacity: int | None = None
        for principal_count in principal_counts:
            selected = [row for row in group_rows if row.get("principal_count") == principal_count]
            namespace = "/".join(str(identity[field]) for field in CAPACITY_GROUP_FIELDS)
            namespace += f"/n={principal_count}"
            estimates = family_stratified_bootstrap(
                selected,
                ("joint_success", "hard_constraint_violation", "worst_principal_regret"),
                samples=samples,
                seed=seed,
                namespace=namespace,
            )
            checks = _gate_diagnostics(estimates)
            cell_pass = bool(selected) and all(checks.values())
            prefix_pass = prefix_pass and cell_pass
            if prefix_pass:
                capacity = principal_count
            per_count.append(
                {
                    "principal_count": principal_count,
                    "n": len(selected),
                    "estimates": estimates,
                    "gate_checks": checks,
                    "cell_pass": cell_pass,
                    "prefix_pass": prefix_pass,
                    "bootstrap_joint_gate_pass_rate_diagnostic": _joint_gate_pass_rate(
                        selected,
                        samples=samples,
                        seed=seed,
                        namespace=namespace,
                    ),
                }
            )
        reports.append(
            {
                **identity,
                "capacity": capacity,
                "capacity_label": str(capacity) if capacity is not None else "below_2",
                "principal_counts": per_count,
                "gates": {
                    "joint_success_rate_min": CAPACITY_JSR_MIN,
                    "hard_constraint_violation_rate_max": CAPACITY_HCVR_MAX,
                    "mean_worst_principal_regret_max": CAPACITY_WORST_REGRET_MAX,
                    "point_and_95_percent_bound_required": True,
                    "all_smaller_counts_required": True,
                },
            }
        )
    return reports


def metric_cells(
    rows: Sequence[Mapping[str, Any]],
    *,
    group_fields: Sequence[str],
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> list[dict[str, Any]]:
    """Summarize primary metrics and resource efficiency by arbitrary frozen cells."""

    grouped: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[tuple(row.get(field) for field in group_fields)].append(row)
    output: list[dict[str, Any]] = []
    primary = (
        "joint_success",
        "hard_constraint_violation",
        "mean_principal_regret",
        "worst_principal_regret",
        "pareto_efficient",
        "initial_json_valid",
        "effective_json_valid",
        "report_consistent",
    )
    for key in sorted(grouped, key=lambda item: tuple(str(value) for value in item)):
        selected = grouped[key]
        identity = dict(zip(group_fields, key, strict=True))
        namespace = "cells/" + "/".join(str(identity[field]) for field in group_fields)
        estimates = family_stratified_bootstrap(
            selected, primary, samples=samples, seed=seed, namespace=namespace
        )
        successes = sum(bool(row.get("joint_success")) for row in selected)

        def per_success(field: str) -> float | None:
            if successes == 0:
                return None
            values = [_finite_number(row.get(field)) for row in selected]
            if any(value is None for value in values):
                return None
            return sum(value for value in values if value is not None) / successes

        output.append(
            {
                **identity,
                "n": len(selected),
                "joint_successes": successes,
                "metrics": estimates,
                "resources_per_success": {
                    "api_calls": per_success("api_calls"),
                    "completion_tokens": per_success("completion_tokens"),
                    "total_tokens": per_success("total_tokens"),
                    "reported_cost_usd": per_success("reported_cost_usd"),
                    "end_to_end_latency_seconds": per_success(
                        "end_to_end_latency_seconds"
                    ),
                },
            }
        )
    return output
