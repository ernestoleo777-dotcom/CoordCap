from __future__ import annotations

import copy

import pytest

from coordcap.assets import build_paper_assets
from coordcap.evaluation import (
    EvaluationError,
    _frozen_scope_audit,
    canonical_sha256,
    coordination_consistency,
    evaluate_documents,
)
from coordcap.statistics import capacity_analysis


def fixture_documents(run_names: list[str]):
    task = {
        "principals": [
            {
                "principal_id": "p1",
                "priority_weight": 1,
                "hard_constraints": [{"constraint_id": "h1"}],
            },
            {
                "principal_id": "p2",
                "priority_weight": 1,
                "hard_constraints": [{"constraint_id": "h2"}],
            },
        ],
        "plans": [
            {"plan_id": "A", "decision": {"resource_allocation": {"choice": "A"}}},
            {"plan_id": "B", "decision": {"resource_allocation": {"choice": "B"}}},
            {"plan_id": "C", "decision": {"resource_allocation": {"choice": "C"}}},
            {"plan_id": "D", "decision": {"resource_allocation": {"choice": "D"}}},
            {"plan_id": "E", "decision": {"resource_allocation": {"choice": "E"}}},
        ],
    }
    oracle = {
        "principal_ids": ["p1", "p2"],
        "priority_weights": {"p1": 1, "p2": 1},
        "feasible_plan_ids": ["A", "B", "C", "E"],
        "utility_bp_by_plan": {
            "A": {"p1": 1000, "p2": 0},
            "B": {"p1": 700, "p2": 700},
            "C": {"p1": 600, "p2": 600},
            "D": {"p1": 900, "p2": 900},
            "E": {"p1": 700, "p2": 700},
        },
        "ideal_bp": {"p1": 1000, "p2": 700},
        "weighted_welfare_by_plan": {
            "A": 1000,
            "B": 1400,
            "C": 1200,
            "D": 1800,
            "E": 1400,
        },
        "pareto_plan_ids": ["A", "B", "E"],
        "pareto_utility_vectors": [[700, 700], [1000, 0]],
        "representative_plan_id": "B",
        "gold_plan_id": "B",
        "representative_weighted_welfare": 1400,
        "gold_weighted_welfare": 1400,
    }
    public_instance = {
        "instance_id": "i1",
        "logical_id": "l1",
        "task_family": "resource_allocation",
        "principal_count": 2,
        "conflict_level": "low",
        "atomic_conflict_density_bp": 200,
        "public_task": task,
        "public_sha256": canonical_sha256(task),
    }
    public = {
        "protocol_version": "coordcap-1.0.0",
        "split": "formal",
        "master_seed": 20260717,
        "instance_count": 1,
        "instances": [public_instance],
    }
    oracle_manifest = {
        "protocol_version": "coordcap-1.0.0",
        "split": "formal",
        "master_seed": 20260717,
        "instance_count": 1,
        "instances": [
            {
                "instance_id": "i1",
                "public_sha256": public_instance["public_sha256"],
                "oracle": oracle,
                "oracle_sha256": canonical_sha256(oracle),
            }
        ],
    }
    runs = [
        {
            "run_id": name,
            "instance_id": "i1",
            "order_variant": "canonical",
            "model": "model",
            "method": "direct_joint_prompt",
            "max_answer_tokens": 512,
            "max_model_calls": 2,
        }
        for name in run_names
    ]
    expected = {
        "protocol_version": "coordcap-1.0.0",
        "public_manifest_sha256": canonical_sha256(public),
        "execution_config_sha256": "e" * 64,
        "runs": runs,
    }
    return public, oracle_manifest, expected, oracle


def terminal(
    expected_run, public_manifest, plan_id="B", *, initial=True, effective=True, abstain=False
):
    resource = {"choice": plan_id} if plan_id in {"A", "B", "C", "D", "E"} else {}
    return {
        **expected_run,
        "protocol_version": public_manifest["protocol_version"],
        "public_manifest_sha256": canonical_sha256(public_manifest),
        "public_sha256": public_manifest["instances"][0]["public_sha256"],
        "execution_config_sha256": "e" * 64,
        "status": "complete" if effective else "invalid_output",
        "initial_json_valid": initial,
        "effective_json_valid": effective,
        "repair_used": effective and not initial,
        "parsed_output": {
            "selected_plan": {"plan_id": plan_id},
            "principal_outcomes": [
                {
                    "principal_id": "p1",
                    "satisfied_hard_constraints": ["h1"],
                    "violated_hard_constraints": [],
                    "utility": None,
                    "justification": "x",
                },
                {
                    "principal_id": "p2",
                    "satisfied_hard_constraints": ["h2"],
                    "violated_hard_constraints": [],
                    "utility": None,
                    "justification": "x",
                },
            ],
            "resource_allocation": resource,
            "unresolved_conflicts": [],
            "abstain": abstain,
        },
        "calls": [
            {
                "raw_path": "raw/call_00.json",
                "usage": {"completion_tokens": 10, "total_tokens": 20},
            }
        ],
        "usage": {"completion_tokens": 10, "total_tokens": 20},
        "usage_complete": True,
        "route_consistent": True,
        "reported_cost": 0.01,
        "latency_seconds": 0.5,
        "budget_compliant": True,
        "raw_paths": ["raw/call_00.json"],
    }


def test_expected_ledger_is_denominator_and_all_failure_kinds_are_retained():
    names = ["success", "invalid", "abstain", "unknown", "infeasible", "missing", "duplicate"]
    public, oracle_manifest, expected, oracle = fixture_documents(names)
    by_id = {run["run_id"]: run for run in expected["runs"]}
    records = [
        terminal(by_id["success"], public, "B"),
        terminal(by_id["invalid"], public, "B", initial=False, effective=False),
        terminal(by_id["abstain"], public, "B", abstain=True),
        terminal(by_id["unknown"], public, "Z"),
        terminal(by_id["infeasible"], public, "D"),
        terminal(by_id["duplicate"], public, "B"),
        terminal(by_id["duplicate"], public, "B"),
    ]
    bundle = evaluate_documents(
        public,
        oracle_manifest,
        expected,
        records,
        recompute_oracle=lambda _: copy.deepcopy(oracle),
        bootstrap_samples=20,
    )
    rows = {row["run_id"]: row for row in bundle["scored_runs"]}
    assert len(rows) == len(expected["runs"])
    assert rows["success"]["joint_success"] == 1
    for run_id in ("invalid", "abstain", "unknown", "infeasible", "missing", "duplicate"):
        assert rows[run_id]["joint_success"] == 0
        assert rows[run_id]["hard_constraint_violation"] == 1
        assert rows[run_id]["mean_principal_regret"] == 1.0
        assert rows[run_id]["worst_principal_regret"] == 1.0
        assert rows[run_id]["pareto_efficient"] == 0
    assert bundle["audit"]["missing_runs"] == 1
    assert bundle["audit"]["duplicate_runs"] == 1
    assert bundle["audit"]["matrix_complete"] is False


def test_model_utility_and_resource_claims_do_not_change_primary_scores():
    public, oracle_manifest, expected, oracle = fixture_documents(["r1"])
    record = terminal(expected["runs"][0], public, "B")
    record["parsed_output"]["principal_outcomes"][0]["utility"] = 999
    record["parsed_output"]["resource_allocation"] = {"fabricated": True}
    bundle = evaluate_documents(
        public,
        oracle_manifest,
        expected,
        [record],
        recompute_oracle=lambda _: oracle,
        bootstrap_samples=10,
    )
    row = bundle["scored_runs"][0]
    assert row["joint_success"] == 1
    assert row["feasible"] is True
    assert row["principal_utilities"] == {"p1": 700, "p2": 700}
    assert row["resource_inconsistent"] is True
    assert row["report_consistent"] == 0


def test_zero_success_resource_efficiency_is_null():
    public, oracle_manifest, expected, oracle = fixture_documents(["r1"])
    record = terminal(expected["runs"][0], public, "C")
    bundle = evaluate_documents(
        public,
        oracle_manifest,
        expected,
        [record],
        recompute_oracle=lambda _: oracle,
        bootstrap_samples=10,
    )
    cell = bundle["metrics"]["aggregate_cells"][0]
    assert cell["joint_successes"] == 0
    assert all(value is None for value in cell["resources_per_success"].values())


def test_top_level_runner_resource_fields_are_summed_per_success():
    public, oracle_manifest, expected, oracle = fixture_documents(["r1"])
    record = terminal(expected["runs"][0], public, "B")
    record.pop("usage")
    record["calls"] = [
        {"call_index": 0, "raw_path": "raw/call_00.json"},
        {"call_index": 1, "raw_path": "raw/call_01.json"},
    ]
    record["raw_paths"] = ["raw/call_00.json", "raw/call_01.json"]
    record["total_prompt_tokens"] = 30
    record["total_completion_tokens"] = 12
    record["reported_cost"] = 0.02
    record["latency_seconds"] = 0.75
    bundle = evaluate_documents(
        public,
        oracle_manifest,
        expected,
        [record],
        recompute_oracle=lambda _: oracle,
        bootstrap_samples=10,
    )
    row = bundle["scored_runs"][0]
    assert row["api_calls"] == 2
    assert row["completion_tokens"] == 12
    assert row["total_tokens"] == 42
    assert row["reported_cost_usd"] == 0.02
    assert row["end_to_end_latency_seconds"] == 0.75
    resources = bundle["metrics"]["aggregate_cells"][0]["resources_per_success"]
    assert resources == {
        "api_calls": 2.0,
        "completion_tokens": 12.0,
        "total_tokens": 42.0,
        "reported_cost_usd": 0.02,
        "end_to_end_latency_seconds": 0.75,
    }


def test_partial_formal_ledger_is_never_publication_ready():
    public, oracle_manifest, expected, oracle = fixture_documents(["r1"])
    bundle = evaluate_documents(
        public,
        oracle_manifest,
        expected,
        [terminal(expected["runs"][0], public, "B")],
        recompute_oracle=lambda _: oracle,
        bootstrap_samples=5,
    )
    assert bundle["audit"]["terminal_matrix_complete"] is True
    assert bundle["audit"]["frozen_scope_complete"] is False
    assert bundle["audit"]["bootstrap_protocol_compliant"] is False
    assert bundle["audit"]["matrix_complete"] is False
    assert bundle["audit"]["publication_ready"] is False


def test_budget_overrun_is_a_terminal_failure_not_a_missing_matrix_row():
    public, oracle_manifest, expected, oracle = fixture_documents(["r1"])
    record = terminal(expected["runs"][0], public, "B")
    record["calls"] = [
        {"call_index": index, "raw_path": f"raw/call_{index:02d}.json"}
        for index in range(3)
    ]
    record["raw_paths"] = [f"raw/call_{index:02d}.json" for index in range(3)]
    record["budget_compliant"] = False
    bundle = evaluate_documents(
        public,
        oracle_manifest,
        expected,
        [record],
        recompute_oracle=lambda _: oracle,
        bootstrap_samples=5,
    )
    row = bundle["scored_runs"][0]
    assert row["protocol_invalid"] is True
    assert row["joint_success"] == 0
    assert row["hard_constraint_violation"] == 1
    assert bundle["audit"]["protocol_invalid_runs"] == 1
    assert bundle["audit"]["terminal_matrix_complete"] is True


def test_route_mismatch_is_a_primary_failure():
    public, oracle_manifest, expected, oracle = fixture_documents(["r1"])
    record = terminal(expected["runs"][0], public, "B")
    record["route_consistent"] = False
    record["status"] = "route_mismatch"
    bundle = evaluate_documents(
        public,
        oracle_manifest,
        expected,
        [record],
        recompute_oracle=lambda _: oracle,
        bootstrap_samples=5,
    )
    row = bundle["scored_runs"][0]
    assert row["protocol_invalid"] is True
    assert row["route_consistent"] is False
    assert row["effective_json_valid"] is False
    assert row["joint_success"] == 0
    assert row["hard_constraint_violation"] == 1
    assert row["mean_principal_regret"] == 1.0
    assert row["worst_principal_regret"] == 1.0


def test_missing_terminal_binding_is_fail_closed():
    public, oracle_manifest, expected, oracle = fixture_documents(["r1"])
    record = terminal(expected["runs"][0], public, "B")
    record.pop("public_sha256")
    bundle = evaluate_documents(
        public,
        oracle_manifest,
        expected,
        [record],
        recompute_oracle=lambda _: oracle,
        bootstrap_samples=5,
    )
    row = bundle["scored_runs"][0]
    assert "public_sha256" in row["record_contract_errors"]
    assert "public_sha256" in row["binding_mismatches"]
    assert row["joint_success"] == 0
    assert bundle["audit"]["terminal_matrix_complete"] is False


def test_execution_config_hash_is_required_and_bound():
    public, oracle_manifest, expected, oracle = fixture_documents(["r1"])
    missing_ledger_binding = copy.deepcopy(expected)
    missing_ledger_binding.pop("execution_config_sha256")
    with pytest.raises(EvaluationError, match="execution_config_sha256"):
        evaluate_documents(
            public,
            oracle_manifest,
            missing_ledger_binding,
            [],
            recompute_oracle=lambda _: oracle,
            bootstrap_samples=5,
        )

    record = terminal(expected["runs"][0], public, "B")
    record["execution_config_sha256"] = "different"
    bundle = evaluate_documents(
        public,
        oracle_manifest,
        expected,
        [record],
        recompute_oracle=lambda _: oracle,
        bootstrap_samples=5,
    )
    row = bundle["scored_runs"][0]
    assert "execution_config_sha256" in row["binding_mismatches"]
    assert row["joint_success"] == 0
    assert bundle["audit"]["terminal_matrix_complete"] is False


def test_frozen_formal_scope_requires_exact_primary_grid_and_reverse_panel():
    families = (
        "resource_allocation",
        "scheduling",
        "shared_plan_selection",
        "policy_choice",
        "constrained_recommendation",
        "conflicting_information_requests",
    )
    counts = (2, 4, 6, 8)
    conflicts = ("low", "medium", "high")
    models = ("google/gemini-3.1-flash-lite", "openai/gpt-5.4-mini")
    methods = (
        "direct_joint_prompt",
        "sequential_aggregation",
        "constraint_ledger",
        "budget_aware_planner",
    )
    instances = []
    by_cell = {}
    for family_index, family in enumerate(families):
        for count_index, principal_count in enumerate(counts):
            for conflict in conflicts:
                for replicate in range(1, 5):
                    instance_id = (
                        f"formal_{family_index}_{count_index}_{conflict}_r{replicate:02d}"
                    )
                    row = {
                        "instance_id": instance_id,
                        "task_family": family,
                        "principal_count": principal_count,
                        "conflict_level": conflict,
                    }
                    instances.append(row)
                    by_cell[(family, principal_count, conflict, replicate)] = row
    runs = []
    for instance in instances:
        for model in models:
            for method in methods:
                for tokens in (512, 1024, 2048):
                    for calls in (1, 2, 4):
                        runs.append(
                            {
                                "instance_id": instance["instance_id"],
                                "order_variant": "canonical",
                                "model": model,
                                "method": method,
                                "max_answer_tokens": tokens,
                                "max_model_calls": calls,
                            }
                        )
    for family_index, family in enumerate(families):
        for count_index, principal_count in enumerate(counts):
            conflict = conflicts[(family_index + count_index) % 3]
            instance = by_cell[(family, principal_count, conflict, 1)]
            for model in models:
                for method in methods:
                    runs.append(
                        {
                            "instance_id": instance["instance_id"],
                            "order_variant": "reverse",
                            "model": model,
                            "method": method,
                            "max_answer_tokens": 1024,
                            "max_model_calls": 2,
                        }
                    )
    manifest = {"split": "formal", "instances": instances}
    report = _frozen_scope_audit(manifest, runs)
    assert report["complete"] is True
    assert report["required_canonical_runs"] == 20_736
    assert report["required_reverse_runs"] == 192
    assert _frozen_scope_audit(manifest, runs[:-1])["complete"] is False


def test_incomplete_asset_builder_emits_na_and_valid_pdfs(tmp_path, monkeypatch):
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "mpl-cache"))
    public, oracle_manifest, expected, oracle = fixture_documents(["r1"])
    bundle = evaluate_documents(
        public,
        oracle_manifest,
        expected,
        [],
        recompute_oracle=lambda _: oracle,
        bootstrap_samples=5,
    )
    paths = build_paper_assets(bundle, tmp_path / "results")
    assert "INCOMPLETE" in paths["primary_table"].read_text(encoding="utf-8")
    assert "PENDING" in paths["paper_macros"].read_text(encoding="utf-8")
    assert "INCOMPLETE" in paths["fact_sheet"].read_text(encoding="utf-8")
    for key in ("performance_figure", "capacity_figure", "failure_figure"):
        assert paths[key].read_bytes().startswith(b"%PDF-")


def test_oracle_corruption_is_rejected_by_recomputation():
    public, oracle_manifest, expected, oracle = fixture_documents(["r1"])
    oracle_manifest["instances"][0]["oracle"]["ideal_bp"]["p1"] = 999
    oracle_manifest["instances"][0]["oracle_sha256"] = canonical_sha256(
        oracle_manifest["instances"][0]["oracle"]
    )
    with pytest.raises(EvaluationError, match="ideal utilities"):
        evaluate_documents(
            public,
            oracle_manifest,
            expected,
            [],
            recompute_oracle=lambda _: oracle,
            bootstrap_samples=5,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("representative_plan_id", "E", "representative_plan_id"),
        ("pareto_utility_vectors", [[1000, 0]], "Pareto utility vectors"),
    ],
)
def test_representative_and_pareto_vector_corruption_is_rejected(field, value, message):
    public, oracle_manifest, expected, oracle = fixture_documents(["r1"])
    oracle_manifest["instances"][0]["oracle"][field] = value
    oracle_manifest["instances"][0]["oracle_sha256"] = canonical_sha256(
        oracle_manifest["instances"][0]["oracle"]
    )
    with pytest.raises(EvaluationError, match=message):
        evaluate_documents(
            public,
            oracle_manifest,
            expected,
            [],
            recompute_oracle=lambda _: oracle,
            bootstrap_samples=5,
        )


def test_coordination_consistency_accepts_same_utility_alternative_plan():
    base = {
        "logical_id": "l1",
        "model": "m",
        "method": "x",
        "max_answer_tokens": 1024,
        "max_model_calls": 2,
        "effective_json_valid": True,
        "feasible": True,
        "principal_utilities": {"p1": 700, "p2": 700},
    }
    result = coordination_consistency(
        [
            {**base, "run_id": "canonical", "order_variant": "canonical"},
            {**base, "run_id": "permuted", "order_variant": "permuted_1"},
        ]
    )
    assert result["pair_count"] == 1
    assert result["rate"] == 1.0


def test_capacity_requires_bounds_and_all_smaller_counts():
    rows = []
    for principal_count, success in ((2, 1), (4, 1), (6, 0), (8, 1)):
        for family in ("a", "b", "c"):
            rows.append(
                {
                    "run_id": f"{principal_count}-{family}",
                    "instance_id": f"{principal_count}-{family}",
                    "logical_id": f"{principal_count}-{family}",
                    "task_family": family,
                    "order_variant": "canonical",
                    "model": "m",
                    "method": "x",
                    "max_answer_tokens": 512,
                    "max_model_calls": 1,
                    "conflict_level": "low",
                    "principal_count": principal_count,
                    "joint_success": success,
                    "hard_constraint_violation": 1 - success,
                    "worst_principal_regret": 0.1 if success else 1.0,
                }
            )
    result = capacity_analysis(rows, samples=20)[0]
    assert result["capacity"] == 4
    assert result["principal_counts"][2]["cell_pass"] is False
    assert result["principal_counts"][3]["prefix_pass"] is False
