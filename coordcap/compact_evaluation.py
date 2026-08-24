"""Gold scoring and gates for the isolated CoordCap compact corrected smoke."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .canonical import canonical_sha256
from .compact_protocol import COMPACT_PROTOCOL_VERSION
from .evaluation import SUCCESS_WELFARE_GAP_MAX, UTILITY_SCALE, normalize_oracle
from .oracle import solve_public_task
from .validation import independent_solve_public_task


FORBIDDEN_PAYLOAD_TERMS = (
    "ideal_utilities",
    "feasible_plan_ids",
    "pareto_plan_ids",
    "representative_plan",
    "weighted_welfare",
    "gold solution",
    "gold utility",
    "oracle manifest",
)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _metric_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "n": 0,
            "joint_success_rate": None,
            "failure_rate": None,
            "hard_constraint_violation_rate": None,
            "pareto_efficiency_rate": None,
            "mean_principal_regret": None,
            "mean_worst_principal_regret": None,
        }

    def mean(field: str) -> float:
        return sum(float(row[field]) for row in rows) / len(rows)

    joint_success_rate = mean("joint_success")
    cost_values = [
        float(row["reported_cost_partial"])
        for row in rows
        if isinstance(row.get("reported_cost_partial"), (int, float))
    ]
    return {
        "n": len(rows),
        "joint_success_rate": joint_success_rate,
        "failure_rate": 1.0 - joint_success_rate,
        "hard_constraint_violation_rate": mean("hard_constraint_violation"),
        "pareto_efficiency_rate": mean("pareto_efficient"),
        "mean_principal_regret": mean("mean_principal_regret"),
        "mean_worst_principal_regret": mean("worst_principal_regret"),
        "direct_json_validity_rate": mean("initial_json_valid"),
        "effective_json_validity_rate": mean("effective_json_valid"),
        "truncated_output_rate": mean("truncated_output"),
        "terminal_transport_failure_rate": mean("terminal_transport_failure"),
        "semantically_scorable_rate": mean("semantic_scorable"),
        "usage_complete_rate": sum(row.get("usage_complete") is True for row in rows)
        / len(rows),
        "repair_count": sum(row.get("repair_used") is True for row in rows),
        "semantic_call_count": sum(int(row.get("semantic_call_count") or 0) for row in rows),
        "network_attempt_count": sum(int(row.get("network_attempt_count") or 0) for row in rows),
        "transport_retry_count": sum(int(row.get("transport_retry_count") or 0) for row in rows),
        "prompt_tokens": sum(int(row.get("prompt_tokens") or 0) for row in rows),
        "completion_tokens": sum(int(row.get("completion_tokens") or 0) for row in rows),
        "reported_cost_usd": sum(cost_values),
        "mean_attempt_latency_seconds": sum(float(row.get("latency_seconds") or 0.0) for row in rows)
        / len(rows),
    }


def _score_terminal(
    terminal: Mapping[str, Any],
    expected: Mapping[str, Any],
    public: Mapping[str, Any],
    oracle: Mapping[str, Any],
    *,
    public_manifest_sha256: str,
    execution_config_sha256: str,
    protocol_version: str = COMPACT_PROTOCOL_VERSION,
) -> dict[str, Any]:
    identity_fields = (
        "run_id",
        "instance_id",
        "model",
        "method",
        "max_output_tokens",
        "max_semantic_calls",
    )
    identity_mismatches = [
        field for field in identity_fields if terminal.get(field) != expected.get(field)
    ]
    binding_errors: list[str] = []
    if terminal.get("protocol_version") != protocol_version:
        binding_errors.append("protocol_version")
    if terminal.get("public_sha256") != public.get("public_sha256"):
        binding_errors.append("public_sha256")
    if terminal.get("public_manifest_sha256") != public_manifest_sha256:
        binding_errors.append("public_manifest_sha256")
    if terminal.get("execution_config_sha256") != execution_config_sha256:
        binding_errors.append("execution_config_sha256")
    view = normalize_oracle(public["public_task"], oracle["oracle"])
    decoded = terminal.get("decoded_decision")
    decoded_value = decoded if isinstance(decoded, Mapping) else {}
    effective_valid = terminal.get("effective_json_valid") is True
    execution_valid = bool(
        not identity_mismatches
        and not binding_errors
        and terminal.get("terminal_transport_failure") is not True
        and terminal.get("route_consistent") is True
    )
    semantic_scorable = bool(effective_valid and execution_valid)
    abstain = bool(decoded_value.get("abstain") is True) if semantic_scorable else False
    plan_id = (
        decoded_value.get("plan_id")
        if semantic_scorable and not abstain and isinstance(decoded_value.get("plan_id"), str)
        else None
    )
    unknown_plan = bool(plan_id is not None and plan_id not in view.plans)
    decision_valid = bool(semantic_scorable and not abstain and plan_id and not unknown_plan)
    feasible = bool(decision_valid and plan_id in view.feasible_plan_ids)
    if feasible and plan_id is not None:
        utilities = dict(view.utilities[plan_id])
        regrets = {
            principal: (view.ideal_utilities[principal] - utilities[principal]) / UTILITY_SCALE
            for principal in view.principal_ids
        }
        mean_regret = sum(regrets.values()) / len(regrets)
        worst_regret = max(regrets.values())
        gap = (view.maximum_weighted_welfare - view.weighted_welfare[plan_id]) / (
            UTILITY_SCALE * sum(view.priority_weights.values())
        )
    else:
        utilities = {}
        regrets = {principal: 1.0 for principal in view.principal_ids}
        mean_regret = 1.0
        worst_regret = 1.0
        gap = None
    joint_success = bool(feasible and gap is not None and gap <= SUCCESS_WELFARE_GAP_MAX)
    pareto = bool(feasible and plan_id in view.pareto_plan_ids)
    return {
        **{field: expected.get(field) for field in identity_fields},
        "task_family": public["task_family"],
        "principal_count": public["principal_count"],
        "conflict_level": public["conflict_level"],
        "status": terminal.get("status"),
        "identity_mismatches": identity_mismatches,
        "binding_errors": binding_errors,
        "initial_json_valid": terminal.get("initial_json_valid") is True,
        "effective_json_valid": effective_valid,
        "truncated_output": terminal.get("truncated_output") is True,
        "terminal_transport_failure": terminal.get("terminal_transport_failure") is True,
        "repair_used": terminal.get("repair_used") is True,
        "semantic_scorable": semantic_scorable,
        "abstain": abstain,
        "selected_plan_id": plan_id,
        "unknown_plan": unknown_plan,
        "feasible": feasible,
        "joint_success": int(joint_success),
        "hard_constraint_violation": int(not feasible),
        "pareto_efficient": int(pareto),
        "principal_utilities": utilities,
        "principal_regrets": regrets,
        "mean_principal_regret": mean_regret,
        "worst_principal_regret": worst_regret,
        "normalized_weighted_welfare_gap": gap,
        "semantic_call_count": terminal.get("semantic_call_count"),
        "network_attempt_count": terminal.get("network_attempt_count"),
        "transport_retry_count": terminal.get("transport_retry_count"),
        "usage_complete": terminal.get("usage_complete"),
        "reported_cost": terminal.get("reported_cost"),
        "reported_cost_partial": terminal.get("reported_cost_partial"),
        "latency_seconds": terminal.get("latency_seconds"),
        "prompt_tokens": terminal.get("total_prompt_tokens"),
        "completion_tokens": terminal.get("total_completion_tokens"),
    }


def evaluate_corrected_smoke(
    *,
    public_path: Path,
    oracle_path: Path,
    expected_path: Path,
    parsed_root: Path,
    raw_root: Path,
) -> dict[str, Any]:
    public_manifest = _load_json(public_path)
    oracle_manifest = _load_json(oracle_path)
    expected = _load_json(expected_path)
    for document in (public_manifest, oracle_manifest, expected):
        if document.get("protocol_version") != COMPACT_PROTOCOL_VERSION:
            raise ValueError("wrong compact protocol version")
    public_hash = _file_sha256(public_path)
    if expected.get("public_manifest_sha256") != public_hash:
        raise ValueError("expected ledger public hash mismatch")
    public_rows = public_manifest.get("instances")
    oracle_rows = oracle_manifest.get("instances")
    expected_runs = expected.get("runs")
    if not isinstance(public_rows, list) or len(public_rows) != 12:
        raise ValueError("corrected public manifest must have 12 tasks")
    if not isinstance(oracle_rows, list) or len(oracle_rows) != 12:
        raise ValueError("corrected oracle manifest must have 12 tasks")
    if not isinstance(expected_runs, list) or len(expected_runs) != 96:
        raise ValueError("corrected expected ledger must have 96 runs")
    public_by_id = {str(row["instance_id"]): row for row in public_rows}
    oracle_by_id = {str(row["instance_id"]): row for row in oracle_rows}

    oracle_validated = 0
    independent_validated = 0
    for instance_id, public in public_by_id.items():
        oracle_row = oracle_by_id[instance_id]
        if oracle_row.get("public_sha256") != public.get("public_sha256"):
            raise ValueError(f"oracle/public binding mismatch: {instance_id}")
        stored = oracle_row["oracle"]
        if canonical_sha256(solve_public_task(public["public_task"])) != canonical_sha256(stored):
            raise ValueError(f"primary oracle recomputation mismatch: {instance_id}")
        oracle_validated += 1
        if canonical_sha256(independent_solve_public_task(public["public_task"])) != canonical_sha256(stored):
            raise ValueError(f"independent oracle mismatch: {instance_id}")
        independent_validated += 1

    expected_by_id = {str(row["run_id"]): row for row in expected_runs}
    if len(expected_by_id) != 96:
        raise ValueError("duplicate expected run ID")
    terminal_by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: list[str] = []
    for path in sorted(parsed_root.glob("*.json")):
        terminal = _load_json(path)
        run_id = str(terminal.get("run_id", ""))
        if run_id in terminal_by_id:
            duplicate_ids.append(run_id)
        terminal_by_id[run_id] = terminal
    unknown_ids = sorted(set(terminal_by_id) - set(expected_by_id))
    missing_ids = sorted(set(expected_by_id) - set(terminal_by_id))
    rows = [
        _score_terminal(
            terminal_by_id[run_id],
            expected_by_id[run_id],
            public_by_id[str(expected_by_id[run_id]["instance_id"])],
            oracle_by_id[str(expected_by_id[run_id]["instance_id"])],
            public_manifest_sha256=public_hash,
            execution_config_sha256=str(expected["execution_config_sha256"]),
        )
        for run_id in sorted(set(expected_by_id) & set(terminal_by_id))
    ]

    raw_files = sorted(raw_root.glob("**/*.json"))
    leakage_findings: list[dict[str, str]] = []
    unique_transport_keys: set[tuple[str, int, int]] = set()
    for path in raw_files:
        raw = _load_json(path)
        payload = raw.get("request_payload")
        response = raw.get("response_payload")
        if not isinstance(payload, Mapping) or not isinstance(response, Mapping):
            raise ValueError(f"raw payload missing: {path}")
        if raw.get("response_payload_sha256") != canonical_sha256(response):
            raise ValueError(f"raw response hash mismatch: {path}")
        key = (
            str(raw.get("run_id")),
            int(raw.get("call_index")),
            int(raw.get("transport_index")),
        )
        if key in unique_transport_keys:
            raise ValueError(f"duplicate transport attempt key: {key}")
        unique_transport_keys.add(key)
        payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True).lower()
        for term in FORBIDDEN_PAYLOAD_TERMS:
            if term in payload_text:
                leakage_findings.append({"path": str(path), "term": term})

    status_counts = Counter(str(row["status"]) for row in rows)
    direct_valid = sum(row["initial_json_valid"] for row in rows)
    effective_valid = sum(row["effective_json_valid"] for row in rows)
    truncated = sum(row["truncated_output"] for row in rows)
    terminal_transport = sum(row["terminal_transport_failure"] for row in rows)
    scorable_by_method = {
        method: sum(row["semantic_scorable"] for row in rows if row["method"] == method)
        for method in sorted({str(row["method"]) for row in rows})
    }
    grouped: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    grouped_model: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    grouped_method: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["model"]), str(row["method"]))].append(row)
        grouped_model[str(row["model"])].append(row)
        grouped_method[str(row["method"])].append(row)

    matrix_complete = bool(
        len(rows) == 96
        and not missing_ids
        and not duplicate_ids
        and not unknown_ids
        and all(not row["identity_mismatches"] and not row["binding_errors"] for row in rows)
    )
    parser_degenerate = effective_valid == 0 or all(
        row["joint_success"] == 0 and row["hard_constraint_violation"] == 1
        for row in rows
    )
    gates = {
        "terminal_records_96_of_96": matrix_complete,
        "terminal_transport_failure_zero": terminal_transport == 0,
        "direct_json_validity_at_least_95pct": direct_valid / 96 >= 0.95,
        "effective_json_validity_at_least_98pct": effective_valid / 96 >= 0.98,
        "truncated_output_at_most_2pct": truncated / 96 <= 0.02,
        "gold_validator_100pct": oracle_validated == 12 and independent_validated == 12,
        "no_oracle_gold_leakage": not leakage_findings,
        "no_silent_exclusion": len(rows) == len(expected_runs),
        "each_method_at_least_10_semantically_scorable": all(
            count >= 10 for count in scorable_by_method.values()
        )
        and len(scorable_by_method) == 4,
        "primary_metrics_not_parser_degenerate": not parser_degenerate,
    }
    go_formal = all(gates.values())
    total_cost_values = [
        float(row["reported_cost_partial"])
        for row in rows
        if isinstance(row.get("reported_cost_partial"), (int, float))
    ]
    return {
        "schema_version": "coordcap-corrected-smoke-metrics-1.1",
        "protocol_version": COMPACT_PROTOCOL_VERSION,
        "decision": "GO_FORMAL" if go_formal else "NO_GO_FORMAL",
        "public_manifest_sha256": public_hash,
        "execution_config_sha256": expected["execution_config_sha256"],
        "audit": {
            "expected_runs": 96,
            "scored_runs": len(rows),
            "missing_run_ids": missing_ids,
            "duplicate_run_ids": duplicate_ids,
            "unknown_run_ids": unknown_ids,
            "raw_transport_attempt_files": len(raw_files),
            "unique_transport_attempt_keys": len(unique_transport_keys),
            "oracle_tasks_validated": oracle_validated,
            "independent_oracle_tasks_validated": independent_validated,
            "payload_leakage_findings": leakage_findings,
            "status_counts": dict(sorted(status_counts.items())),
            "terminal_transport_failures": terminal_transport,
            "truncated_outputs": truncated,
            "direct_json_valid": direct_valid,
            "effective_json_valid": effective_valid,
            "semantically_scorable_by_method": scorable_by_method,
        },
        "gates": gates,
        "metrics": {
            "overall": _metric_summary(rows),
            "by_model": [
                {"model": model, **_metric_summary(group_rows)}
                for model, group_rows in sorted(grouped_model.items())
            ],
            "by_method": [
                {"method": method, **_metric_summary(group_rows)}
                for method, group_rows in sorted(grouped_method.items())
            ],
            "by_model_method": [
                {
                    "model": model,
                    "method": method,
                    **_metric_summary(group_rows),
                    "semantically_scorable": sum(
                        row["semantic_scorable"] for row in group_rows
                    ),
                }
                for (model, method), group_rows in sorted(grouped.items())
            ],
            "direct_json_validity_rate": direct_valid / 96,
            "effective_json_validity_rate": effective_valid / 96,
            "truncated_output_rate": truncated / 96,
            "reported_cost_usd_lower_bound": sum(total_cost_values),
        },
        "scored_runs": rows,
    }


def write_corrected_metrics(bundle: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(bundle), indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
