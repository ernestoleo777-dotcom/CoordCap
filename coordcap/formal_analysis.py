"""Frozen Phase 3A local references, scoring, bootstrap, capacity, and gates."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .canonical import canonical_sha256, derive_seed
from .compact_evaluation import (
    FORBIDDEN_PAYLOAD_TERMS,
    _metric_summary,
    _score_terminal,
)
from .compact_protocol import FORMAL_MAIN_PROTOCOL_VERSION
from .oracle import solve_public_task
from .statistics import (
    BOOTSTRAP_SAMPLES,
    BOOTSTRAP_SEED,
    CAPACITY_HCVR_MAX,
    CAPACITY_JSR_MIN,
    CAPACITY_WORST_REGRET_MAX,
    family_stratified_bootstrap,
)
from .validation import independent_solve_public_task


FORMAL_PRIMARY_FIELDS = (
    "joint_success",
    "hard_constraint_violation",
    "mean_principal_regret",
    "worst_principal_regret",
    "pareto_efficient",
)
FORMAL_GROUPINGS = (
    ("model", "method"),
    ("method", "principal_count"),
    ("method", "conflict_level"),
    ("model", "principal_count"),
    ("principal_count", "conflict_level"),
    ("task_family",),
    ("hard_constraint_count",),
)
FORMAL_SIGNAL_MIN_EFFECT = 0.05


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hard_constraint_count(public_task: Mapping[str, Any]) -> int:
    shared = public_task.get("shared_constraints")
    principals = public_task.get("principals")
    if not isinstance(shared, list) or not isinstance(principals, list):
        raise ValueError("public task constraint structure missing")
    total = len(shared)
    for principal in principals:
        if not isinstance(principal, Mapping) or not isinstance(principal.get("hard_constraints"), list):
            raise ValueError("principal hard constraints missing")
        total += len(principal["hard_constraints"])
    return total


def _score_local_plan(
    *,
    public: Mapping[str, Any],
    oracle: Mapping[str, Any],
    plan_id: str,
    label: str,
) -> dict[str, Any]:
    expected = {
        "run_id": f"local_{label}_{public['instance_id']}",
        "instance_id": public["instance_id"],
        "model": "local_reference",
        "method": label,
        "max_output_tokens": 0,
        "max_semantic_calls": 0,
    }
    terminal = {
        **expected,
        "protocol_version": FORMAL_MAIN_PROTOCOL_VERSION,
        "public_sha256": public["public_sha256"],
        "public_manifest_sha256": "local_reference",
        "execution_config_sha256": "local_reference",
        "effective_json_valid": True,
        "initial_json_valid": True,
        "decoded_decision": {"plan_id": plan_id, "abstain": False},
        "terminal_transport_failure": False,
        "route_consistent": True,
        "status": "complete",
        "truncated_output": False,
        "repair_used": False,
        "semantic_call_count": 0,
        "network_attempt_count": 0,
        "transport_retry_count": 0,
        "usage_complete": True,
        "reported_cost": 0.0,
        "reported_cost_partial": 0.0,
        "latency_seconds": 0.0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
    }
    row = _score_terminal(
        terminal,
        expected,
        public,
        oracle,
        public_manifest_sha256="local_reference",
        execution_config_sha256="local_reference",
        protocol_version=FORMAL_MAIN_PROTOCOL_VERSION,
    )
    row["hard_constraint_count"] = hard_constraint_count(public["public_task"])
    return row


def build_local_references(
    public_manifest: Mapping[str, Any],
    oracle_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    public_rows = public_manifest["instances"]
    oracle_by_id = {row["instance_id"]: row for row in oracle_manifest["instances"]}
    output_rows: list[dict[str, Any]] = []
    scored: list[dict[str, Any]] = []
    for public in public_rows:
        oracle_row = oracle_by_id[public["instance_id"]]
        recomputed = solve_public_task(public["public_task"])
        independent = independent_solve_public_task(public["public_task"])
        if canonical_sha256(recomputed) != canonical_sha256(oracle_row["oracle"]):
            raise ValueError(f"oracle recomputation mismatch: {public['instance_id']}")
        if canonical_sha256(independent) != canonical_sha256(oracle_row["oracle"]):
            raise ValueError(f"independent oracle mismatch: {public['instance_id']}")
        oracle = recomputed
        feasible = list(oracle["feasible_plan_ids"])
        if not feasible:
            raise ValueError(f"no feasible plan: {public['instance_id']}")
        plan_order = [plan["plan_id"] for plan in public["public_task"]["plans"]]
        order_index = {plan_id: index for index, plan_id in enumerate(plan_order)}
        principals = list(oracle["principal_ids"])
        greedy = max(
            feasible,
            key=lambda plan_id: (
                tuple(int(oracle["utility_bp_by_plan"][plan_id][principal]) for principal in principals),
                -order_index[plan_id],
            ),
        )
        random_seed = derive_seed(
            int(public_manifest["master_seed"]), "formal-random-valid", public["instance_id"]
        )
        random_valid = feasible[random_seed % len(feasible)]
        selections = {
            "oracle_feasible": str(oracle["representative_plan_id"]),
            "greedy_feasible": greedy,
            "random_valid": random_valid,
        }
        output_rows.append(
            {
                "instance_id": public["instance_id"],
                "task_family": public["task_family"],
                "principal_count": public["principal_count"],
                "conflict_level": public["conflict_level"],
                "hard_constraint_count": hard_constraint_count(public["public_task"]),
                "feasible_plan_count": len(feasible),
                "pareto_plan_count": len(oracle["pareto_plan_ids"]),
                "selections": selections,
            }
        )
        for label, plan_id in selections.items():
            scored.append(
                _score_local_plan(
                    public=public,
                    oracle=oracle_row,
                    plan_id=plan_id,
                    label=label,
                )
            )
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in scored:
        grouped[str(row["method"])].append(row)
    return {
        "schema_version": "coordcap-local-reference-baselines-1.0",
        "protocol_version": FORMAL_MAIN_PROTOCOL_VERSION,
        "task_count": len(output_rows),
        "api_calls_made": 0,
        "definitions": {
            "oracle_feasible": "local solver representative maximum-welfare feasible plan",
            "greedy_feasible": "filter hard-feasible plans, then lexicographically maximize principal utilities in frozen principal order with public plan-order tie break",
            "random_valid": "uniform-index selection from the feasible set using a fixed per-instance derived seed",
        },
        "overall": {
            label: _metric_summary(rows) for label, rows in sorted(grouped.items())
        },
        "tasks": output_rows,
    }


def _percentile(values: Sequence[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * p
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _contrast_seed(namespace: str) -> int:
    digest = hashlib.sha256(
        f"coordcap-formal-contrast\0{BOOTSTRAP_SEED}\0{namespace}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big")


def bootstrap_absolute_difference(
    left: Sequence[Mapping[str, Any]],
    right: Sequence[Mapping[str, Any]],
    field: str,
    *,
    namespace: str,
    samples: int = BOOTSTRAP_SAMPLES,
) -> dict[str, Any]:
    def episode_values(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
        grouped: defaultdict[str, list[float]] = defaultdict(list)
        for row in rows:
            value = row.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                grouped[str(row["instance_id"])].append(float(value))
        return {key: sum(values) / len(values) for key, values in grouped.items()}

    a = episode_values(left)
    b = episode_values(right)
    if not a or not b:
        return {"difference": None, "lower_95": None, "upper_95": None, "bootstrap_samples": 0}
    point = sum(a.values()) / len(a) - sum(b.values()) / len(b)
    rng = random.Random(_contrast_seed(namespace))
    draws: list[float] = []
    shared = sorted(set(a) & set(b))
    if shared and set(a) == set(b):
        diffs = [a[key] - b[key] for key in shared]
        for _ in range(samples):
            draws.append(sum(diffs[rng.randrange(len(diffs))] for _ in diffs) / len(diffs))
    else:
        av = list(a.values())
        bv = list(b.values())
        for _ in range(samples):
            am = sum(av[rng.randrange(len(av))] for _ in av) / len(av)
            bm = sum(bv[rng.randrange(len(bv))] for _ in bv) / len(bv)
            draws.append(am - bm)
    return {
        "difference": point,
        "lower_95": _percentile(draws, 0.025),
        "upper_95": _percentile(draws, 0.975),
        "bootstrap_samples": samples,
        "left_episodes": len(a),
        "right_episodes": len(b),
    }


def _group_key(row: Mapping[str, Any], fields: Sequence[str]) -> tuple[Any, ...]:
    return tuple(row.get(field) for field in fields)


def stratified_analysis(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    output: list[dict[str, Any]] = []
    bootstrap: dict[str, Any] = {}
    for fields in FORMAL_GROUPINGS:
        grouped: defaultdict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[_group_key(row, fields)].append(row)
        for key, group_rows in sorted(grouped.items(), key=lambda item: tuple(str(v) for v in item[0])):
            identity = dict(zip(fields, key, strict=True))
            for view, view_rows in (
                ("all_attempt", group_rows),
                (
                    "execution_conditioned",
                    [row for row in group_rows if row.get("semantic_scorable") is True],
                ),
            ):
                summary = _metric_summary(view_rows)
                output.append(
                    {
                        "grouping": "_x_".join(fields),
                        "view": view,
                        **{field: identity.get(field) for field in fields},
                        **summary,
                    }
                )
                namespace = "/".join(f"{field}={identity[field]}" for field in fields) + f"/{view}"
                bootstrap[namespace] = family_stratified_bootstrap(
                    view_rows,
                    FORMAL_PRIMARY_FIELDS,
                    samples=BOOTSTRAP_SAMPLES,
                    seed=BOOTSTRAP_SEED,
                    namespace=namespace,
                )
    return output, bootstrap


def capacity_analysis_formal(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    models = sorted({str(row["model"]) for row in rows})
    methods = sorted({str(row["method"]) for row in rows})
    for model in models:
        for method in methods:
            prefix = True
            capacity: int | None = None
            per_count: list[dict[str, Any]] = []
            for count in (2, 4, 6, 8):
                selected = [
                    row for row in rows
                    if row["model"] == model and row["method"] == method
                    and row["principal_count"] == count
                ]
                estimates = family_stratified_bootstrap(
                    selected,
                    ("joint_success", "hard_constraint_violation", "worst_principal_regret"),
                    namespace=f"capacity/{model}/{method}/{count}",
                )
                checks = {
                    "jsr_point": estimates["joint_success"]["point"] is not None
                    and estimates["joint_success"]["point"] >= CAPACITY_JSR_MIN,
                    "jsr_lower_95": estimates["joint_success"]["lower_95"] is not None
                    and estimates["joint_success"]["lower_95"] >= CAPACITY_JSR_MIN,
                    "hcvr_point": estimates["hard_constraint_violation"]["point"] is not None
                    and estimates["hard_constraint_violation"]["point"] <= CAPACITY_HCVR_MAX,
                    "hcvr_upper_95": estimates["hard_constraint_violation"]["upper_95"] is not None
                    and estimates["hard_constraint_violation"]["upper_95"] <= CAPACITY_HCVR_MAX,
                    "worst_regret_point": estimates["worst_principal_regret"]["point"] is not None
                    and estimates["worst_principal_regret"]["point"] <= CAPACITY_WORST_REGRET_MAX,
                    "worst_regret_upper_95": estimates["worst_principal_regret"]["upper_95"] is not None
                    and estimates["worst_principal_regret"]["upper_95"] <= CAPACITY_WORST_REGRET_MAX,
                }
                cell_pass = all(checks.values())
                prefix = prefix and cell_pass
                if prefix:
                    capacity = count
                per_count.append(
                    {"principal_count": count, "estimates": estimates, "checks": checks, "cell_pass": cell_pass, "prefix_pass": prefix}
                )
            reports.append(
                {"model": model, "method": method, "coordination_capacity": capacity, "per_count": per_count}
            )
    return reports


def _planned_contrasts(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    p2 = [row for row in rows if row["principal_count"] == 2]
    p8 = [row for row in rows if row["principal_count"] == 8]
    low = [row for row in rows if row["conflict_level"] == "low"]
    high = [row for row in rows if row["conflict_level"] == "high"]
    gemini = [row for row in rows if str(row["model"]).startswith("google/")]
    gpt = [row for row in rows if str(row["model"]).startswith("openai/")]
    contrasts = {
        "principal_8_minus_2": {
            field: bootstrap_absolute_difference(p8, p2, field, namespace=f"p8-p2/{field}")
            for field in FORMAL_PRIMARY_FIELDS
        },
        "conflict_high_minus_low": {
            field: bootstrap_absolute_difference(high, low, field, namespace=f"high-low/{field}")
            for field in FORMAL_PRIMARY_FIELDS
        },
        "gemini_minus_gpt": {
            field: bootstrap_absolute_difference(gemini, gpt, field, namespace=f"gemini-gpt/{field}")
            for field in FORMAL_PRIMARY_FIELDS
        },
        "method_minus_direct": {},
    }
    direct = [row for row in rows if row["method"] == "direct_joint_prompt"]
    for method in ("sequential_aggregation", "constraint_ledger", "budget_aware_planner"):
        selected = [row for row in rows if row["method"] == method]
        contrasts["method_minus_direct"][method] = {
            field: bootstrap_absolute_difference(
                selected, direct, field, namespace=f"{method}-direct/{field}"
            )
            for field in FORMAL_PRIMARY_FIELDS
        }
    return contrasts


def signal_decision(contrasts: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    phenomena: list[dict[str, Any]] = []

    def supported(name: str, contrast: Mapping[str, Any], direction: int) -> None:
        value = contrast.get("difference")
        lower = contrast.get("lower_95")
        upper = contrast.get("upper_95")
        effect = abs(float(value)) if isinstance(value, (int, float)) else 0.0
        ci_support = bool(
            direction > 0 and isinstance(lower, (int, float)) and lower > 0
            or direction < 0 and isinstance(upper, (int, float)) and upper < 0
        )
        phenomena.append(
            {"phenomenon": name, "effect": value, "nontrivial": effect >= FORMAL_SIGNAL_MIN_EFFECT, "ci_support": ci_support}
        )

    supported(
        "principal_count_increases_regret",
        contrasts["principal_8_minus_2"]["mean_principal_regret"],
        1,
    )
    supported(
        "principal_count_reduces_jsr",
        contrasts["principal_8_minus_2"]["joint_success"],
        -1,
    )
    supported(
        "conflict_increases_violation",
        contrasts["conflict_high_minus_low"]["hard_constraint_violation"],
        1,
    )
    supported(
        "conflict_increases_regret",
        contrasts["conflict_high_minus_low"]["mean_principal_regret"],
        1,
    )
    supported(
        "model_jsr_difference",
        contrasts["gemini_minus_gpt"]["joint_success"],
        1 if (contrasts["gemini_minus_gpt"]["joint_success"].get("difference") or 0) >= 0 else -1,
    )
    for method, values in contrasts["method_minus_direct"].items():
        supported(
            f"{method}_jsr_minus_direct",
            values["joint_success"],
            1,
        )
        supported(
            f"{method}_regret_minus_direct",
            values["mean_principal_regret"],
            -1,
        )
    if any(row["nontrivial"] and row["ci_support"] for row in phenomena):
        return "STRONG_SIGNAL", phenomena
    if any(row["nontrivial"] for row in phenomena[:4]):
        return "DIAGNOSTIC_SIGNAL", phenomena
    return "WEAK_SIGNAL", phenomena


def evaluate_formal_main(
    *,
    public_path: Path,
    oracle_path: Path,
    expected_path: Path,
    parsed_root: Path,
    raw_root: Path,
) -> dict[str, Any]:
    public_manifest = _load(public_path)
    oracle_manifest = _load(oracle_path)
    expected = _load(expected_path)
    if any(
        document.get("protocol_version") != FORMAL_MAIN_PROTOCOL_VERSION
        for document in (public_manifest, oracle_manifest, expected)
    ):
        raise ValueError("formal main protocol mismatch")
    public_hash = _file_sha256(public_path)
    if expected.get("public_manifest_sha256") != public_hash:
        raise ValueError("formal expected ledger public hash mismatch")
    public_rows = public_manifest.get("instances")
    oracle_rows = oracle_manifest.get("instances")
    expected_runs = expected.get("runs")
    if not isinstance(public_rows, list) or len(public_rows) != 80:
        raise ValueError("formal public manifest must contain 80 tasks")
    if not isinstance(oracle_rows, list) or len(oracle_rows) != 80:
        raise ValueError("formal oracle manifest must contain 80 tasks")
    if not isinstance(expected_runs, list) or len(expected_runs) != 640:
        raise ValueError("formal expected ledger must contain 640 runs")
    public_by_id = {str(row["instance_id"]): row for row in public_rows}
    oracle_by_id = {str(row["instance_id"]): row for row in oracle_rows}
    for instance_id, public in public_by_id.items():
        stored = oracle_by_id[instance_id]["oracle"]
        if canonical_sha256(solve_public_task(public["public_task"])) != canonical_sha256(stored):
            raise ValueError(f"formal oracle recomputation mismatch: {instance_id}")
        if canonical_sha256(independent_solve_public_task(public["public_task"])) != canonical_sha256(stored):
            raise ValueError(f"formal independent oracle mismatch: {instance_id}")

    expected_by_id = {str(row["run_id"]): row for row in expected_runs}
    terminals: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for path in sorted(parsed_root.glob("*.json")):
        terminal = _load(path)
        run_id = str(terminal.get("run_id", ""))
        if run_id in terminals:
            duplicates.append(run_id)
        terminals[run_id] = terminal
    missing = sorted(set(expected_by_id) - set(terminals))
    unknown = sorted(set(terminals) - set(expected_by_id))
    rows: list[dict[str, Any]] = []
    for run_id in sorted(set(expected_by_id) & set(terminals)):
        expected_row = expected_by_id[run_id]
        instance_id = str(expected_row["instance_id"])
        row = _score_terminal(
            terminals[run_id],
            expected_row,
            public_by_id[instance_id],
            oracle_by_id[instance_id],
            public_manifest_sha256=public_hash,
            execution_config_sha256=str(expected["execution_config_sha256"]),
            protocol_version=FORMAL_MAIN_PROTOCOL_VERSION,
        )
        row["hard_constraint_count"] = hard_constraint_count(
            public_by_id[instance_id]["public_task"]
        )
        row["execution_observed"] = True
        row["missing_execution"] = False
        rows.append(row)
    for run_id in missing:
        expected_row = expected_by_id[run_id]
        instance_id = str(expected_row["instance_id"])
        public = public_by_id[instance_id]
        rows.append(
            {
                **expected_row,
                "task_family": public["task_family"],
                "principal_count": public["principal_count"],
                "conflict_level": public["conflict_level"],
                "hard_constraint_count": hard_constraint_count(public["public_task"]),
                "status": "not_executed_safe_stop",
                "identity_mismatches": [],
                "binding_errors": [],
                "initial_json_valid": False,
                "effective_json_valid": False,
                "truncated_output": False,
                "terminal_transport_failure": False,
                "repair_used": False,
                "semantic_scorable": False,
                "abstain": False,
                "selected_plan_id": None,
                "unknown_plan": False,
                "feasible": False,
                "joint_success": 0,
                "hard_constraint_violation": 1,
                "pareto_efficient": 0,
                "principal_utilities": {},
                "principal_regrets": {},
                "mean_principal_regret": 1.0,
                "worst_principal_regret": 1.0,
                "normalized_weighted_welfare_gap": None,
                "semantic_call_count": 0,
                "network_attempt_count": 0,
                "transport_retry_count": 0,
                "usage_complete": False,
                "reported_cost": None,
                "reported_cost_partial": 0.0,
                "latency_seconds": 0.0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "execution_observed": False,
                "missing_execution": True,
            }
        )
    rows.sort(key=lambda row: str(row["run_id"]))

    leakage: list[dict[str, str]] = []
    raw_files = sorted(raw_root.glob("**/*.json"))
    unique_transport: set[tuple[str, int, int]] = set()
    for path in raw_files:
        raw = _load(path)
        payload = raw.get("request_payload")
        response = raw.get("response_payload")
        if not isinstance(payload, Mapping) or not isinstance(response, Mapping):
            raise ValueError(f"raw payload missing: {path}")
        if raw.get("response_payload_sha256") != canonical_sha256(response):
            raise ValueError(f"raw response hash mismatch: {path}")
        key = (str(raw.get("run_id")), int(raw.get("call_index")), int(raw.get("transport_index")))
        if key in unique_transport:
            raise ValueError(f"duplicate transport key: {key}")
        unique_transport.add(key)
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True).lower()
        for term in FORBIDDEN_PAYLOAD_TERMS:
            if term in text:
                leakage.append({"path": str(path), "term": term})

    stratified, bootstrap = stratified_analysis(rows)
    capacity = capacity_analysis_formal(rows)
    contrasts = _planned_contrasts(rows)
    signal, phenomena = signal_decision(contrasts)
    overall = _metric_summary(rows)
    scorable = sum(row["semantic_scorable"] for row in rows)
    effective = sum(row["effective_json_valid"] for row in rows)
    truncated = sum(row["truncated_output"] for row in rows)
    transport = sum(row["terminal_transport_failure"] for row in rows)
    cost = sum(float(row.get("reported_cost_partial") or 0.0) for row in rows)
    gates = {
        "terminal_records_640_of_640": len(terminals) == 640 and not missing and not unknown and not duplicates,
        "semantically_scorable_at_least_98pct": scorable / 640 >= 0.98,
        "effective_json_validity_at_least_98pct": effective / 640 >= 0.98,
        "truncated_output_at_most_2pct": truncated / 640 <= 0.02,
        "terminal_transport_failure_at_most_1pct": transport / 640 <= 0.01,
        "gold_evaluator_tests_100pct": len(public_by_id) == 80,
        "no_leakage": not leakage,
        "no_silent_exclusion": len(rows) == len(expected_runs),
        "actual_cost_at_most_5_usd": cost <= 5.0,
        "preregistered_strata_generated": bool(stratified) and all(FORMAL_GROUPINGS),
    }
    if not all(gates.values()):
        signal = "WEAK_SIGNAL"
        phenomena.insert(
            0,
            {
                "phenomenon": "formal_gate_incomplete_forces_weak_signal",
                "effect": None,
                "nontrivial": False,
                "ci_support": False,
            },
        )
    success_count = sum(row["joint_success"] for row in rows)
    efficiency = {
        "successful_tasks": success_count,
        "tokens_per_successful_task": (
            sum(int(row.get("prompt_tokens") or 0) + int(row.get("completion_tokens") or 0) for row in rows) / success_count
            if success_count else None
        ),
        "cost_per_successful_task_usd": cost / success_count if success_count else None,
        "latency_per_successful_task_seconds": (
            sum(float(row.get("latency_seconds") or 0.0) for row in rows) / success_count
            if success_count else None
        ),
    }
    return {
        "schema_version": "coordcap-formal-main-metrics-1.0",
        "protocol_version": FORMAL_MAIN_PROTOCOL_VERSION,
        "public_manifest_sha256": public_hash,
        "execution_config_sha256": expected["execution_config_sha256"],
        "gate_pass": all(gates.values()),
        "gates": gates,
        "signal_decision": signal,
        "signal_phenomena": phenomena,
        "coordination_consistency": {
            "status": "not_estimable",
            "reason": "The authorized 640-attempt main matrix has one canonical-order episode per task/model/method and no repeated or order-perturbed pair. No extra API calls are authorized for this metric.",
        },
        "metrics": {
            "overall_all_attempt": overall,
            "overall_execution_conditioned": _metric_summary(
                [row for row in rows if row["semantic_scorable"]]
            ),
            "efficiency": efficiency,
            "reported_cost_usd": cost,
            "semantic_scorable_rate": scorable / 640,
            "effective_json_validity_rate": effective / 640,
            "truncated_output_rate": truncated / 640,
            "terminal_transport_failure_rate": transport / 640,
        },
        "audit": {
            "expected_runs": 640,
            "scored_runs": len(rows),
            "observed_terminal_records": len(terminals),
            "missing_run_ids": missing,
            "unknown_run_ids": unknown,
            "duplicate_run_ids": duplicates,
            "raw_transport_attempt_files": len(raw_files),
            "unique_transport_attempt_keys": len(unique_transport),
            "payload_leakage_findings": leakage,
            "status_counts": dict(sorted(Counter(str(row["status"]) for row in rows).items())),
        },
        "capacity": capacity,
        "contrasts": contrasts,
        "bootstrap": bootstrap,
        "stratified": stratified,
        "scored_attempts": rows,
    }


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({str(key) for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value
                    for key, value in row.items()
                }
            )
