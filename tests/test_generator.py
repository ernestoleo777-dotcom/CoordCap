from __future__ import annotations

import copy
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from coordcap.canonical import canonical_bytes, canonical_sha256, derive_seed, load_strict_json
from coordcap.evaluation import canonical_sha256 as evaluator_sha256
from coordcap.oracle import solve_public_task
from coordcap.tasks import (
    CONFLICT_INTERVAL_BP,
    CONFLICT_LEVELS,
    CANDIDATE_PLAN_COUNT,
    FAMILY_SPECS,
    FORMAL_PRINCIPAL_COUNTS,
    SMOKE_PRINCIPAL_COUNTS,
    TASK_FAMILIES,
    build_public_task,
    generate_manifest_pair,
    merge_manifest_pairs,
)
from coordcap.validation import (
    ValidationError,
    assert_public_safe,
    independent_solve_public_task,
    scan_public_leakage,
    validate_manifest_pair,
)


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "generate_coordcap.py"
VALIDATOR = ROOT / "scripts" / "validate_manifest.py"


@pytest.fixture(scope="module")
def smoke_pair():
    return generate_manifest_pair("smoke")


def test_smoke_has_frozen_size_and_required_coverage(smoke_pair) -> None:
    public, oracle = smoke_pair
    assert public["instance_count"] == oracle["instance_count"] == 40
    assert {row["task_family"] for row in public["instances"]} == set(TASK_FAMILIES)
    assert {row["principal_count"] for row in public["instances"]} == set(
        SMOKE_PRINCIPAL_COUNTS
    )
    assert {row["conflict_level"] for row in public["instances"]} == set(
        CONFLICT_LEVELS
    )
    family_conflicts = {
        (row["task_family"], row["conflict_level"]) for row in public["instances"]
    }
    assert family_conflicts == set((family, level) for family in TASK_FAMILIES for level in CONFLICT_LEVELS)


def test_formal_is_exact_balanced_288_matrix() -> None:
    public, oracle = generate_manifest_pair("formal")
    assert public["instance_count"] == oracle["instance_count"] == 288
    cells = Counter(
        (row["task_family"], row["principal_count"], row["conflict_level"])
        for row in public["instances"]
    )
    assert len(cells) == 6 * 4 * 3
    assert set(cells.values()) == {4}
    assert {cell[1] for cell in cells} == set(FORMAL_PRINCIPAL_COUNTS)
    # Full formal validation includes public-only answerability and independent
    # brute-force agreement for every instance.
    report = validate_manifest_pair(public, oracle)
    assert report["status"] == "pass"
    assert report["solver_agreements"] == 288


def test_public_and_oracle_are_strictly_separated_and_hash_bound(smoke_pair) -> None:
    public, oracle = smoke_pair
    for public_row, oracle_row in zip(public["instances"], oracle["instances"], strict=True):
        assert set(public_row) == {
            "instance_id",
            "logical_id",
            "task_family",
            "principal_count",
            "conflict_level",
            "atomic_conflict_density_bp",
            "public_task",
            "public_sha256",
        }
        assert set(oracle_row) == {
            "instance_id",
            "public_sha256",
            "oracle",
            "oracle_sha256",
        }
        assert scan_public_leakage(public_row["public_task"]) == []
        assert canonical_sha256(public_row["public_task"]) == public_row["public_sha256"]
        assert evaluator_sha256(public_row["public_task"]) == public_row["public_sha256"]
        assert oracle_row["public_sha256"] == public_row["public_sha256"]
        assert canonical_sha256(oracle_row["oracle"]) == oracle_row["oracle_sha256"]
        public_text = canonical_bytes(public_row["public_task"]).decode("utf-8").lower()
        for token in (
            "gold",
            "utility",
            "regret",
            "pareto",
            "answer",
            "optimal",
            "representative",
            "welfare",
            "feasible",
        ):
            assert token not in public_text


def test_leakage_canary_rejects_oracle_vocabulary(smoke_pair) -> None:
    public, _oracle = smoke_pair
    contaminated = copy.deepcopy(public["instances"][0]["public_task"])
    contaminated["gold_answer"] = {"plan_id": "canary"}
    with pytest.raises(ValidationError, match="leakage canary"):
        assert_public_safe(contaminated)


def test_every_smoke_oracle_has_required_finite_domain_properties(smoke_pair) -> None:
    public, oracle = smoke_pair
    for public_row, oracle_row in zip(public["instances"], oracle["instances"], strict=True):
        task = public_row["public_task"]
        stored = oracle_row["oracle"]
        primary = solve_public_task(task)
        independent = independent_solve_public_task(task)
        assert stored == primary == independent
        feasible = stored["feasible_plan_ids"]
        assert len(task["plans"]) == CANDIDATE_PLAN_COUNT == 48
        assert len(feasible) >= 2
        vectors = {
            tuple(stored["utility_bp_by_plan"][plan_id][pid] for pid in stored["principal_ids"])
            for plan_id in feasible
        }
        assert len(vectors) >= 2
        assert stored["dominated_feasible_plan_ids"]
        assert stored["representative_plan_id"] in feasible
        assert stored["representative_plan_id"] == stored["gold_plan_id"]
        assert all(
            0 <= value <= 1000
            for plan_id in stored["domain_plan_ids"]
            for value in stored["utility_bp_by_plan"][plan_id].values()
        )
        assert all(
            isinstance(plan["decision"]["resource_allocation"], dict)
            for plan in task["plans"]
        )
        assert all(
            constraint["field"].startswith("decision.resource_allocation.")
            and constraint["operator"] == "at_most"
            and isinstance(constraint["value"], int)
            for constraint in task["shared_constraints"]
        )


def test_conflict_density_and_graph_are_atomic_and_in_band(smoke_pair) -> None:
    public, _oracle = smoke_pair
    for row in public["instances"]:
        conflict = row["public_task"]["conflict"]
        assert len(conflict["issue_ids"]) == 7
        expected = round(
            10000 * conflict["atomic_conflicts"] / conflict["atomic_opportunities"]
        )
        assert conflict["atomic_density_bp"] == expected
        assert row["atomic_conflict_density_bp"] == expected
        low, high = CONFLICT_INTERVAL_BP[row["conflict_level"]]
        assert low <= expected <= high
        assert all(1 <= edge["atomic_conflicts"] <= 7 for edge in conflict["graph"])


def test_plan_order_uses_independent_seed_and_does_not_change_oracle() -> None:
    instance_seed = derive_seed(20260717, "fixed-task")
    first = build_public_task(
        family="scheduling",
        principal_count=6,
        conflict_level="high",
        instance_seed=instance_seed,
        plan_order_seed=derive_seed(1, "order"),
    )
    second = build_public_task(
        family="scheduling",
        principal_count=6,
        conflict_level="high",
        instance_seed=instance_seed,
        plan_order_seed=derive_seed(2, "order"),
    )
    first_order = [plan["plan_id"] for plan in first["plans"]]
    second_order = [plan["plan_id"] for plan in second["plans"]]
    assert first_order != second_order
    assert {plan["plan_id"] for plan in first["plans"]} == {
        plan["plan_id"] for plan in second["plans"]
    }
    assert solve_public_task(first) == solve_public_task(second)


def test_same_seed_reverse_generation_and_shards_are_byte_identical(smoke_pair) -> None:
    canonical_pair = smoke_pair
    reverse_pair = generate_manifest_pair("smoke", generation_order=list(reversed(range(40))))
    assert canonical_bytes(canonical_pair[0]) == canonical_bytes(reverse_pair[0])
    assert canonical_bytes(canonical_pair[1]) == canonical_bytes(reverse_pair[1])
    shards = [
        generate_manifest_pair("smoke", shard_index=index, shard_count=4)
        for index in range(4)
    ]
    merged = merge_manifest_pairs(list(reversed(shards)))
    assert canonical_bytes(canonical_pair[0]) == canonical_bytes(merged[0])
    assert canonical_bytes(canonical_pair[1]) == canonical_bytes(merged[1])


def test_pythonhashseed_and_cli_outputs_are_byte_identical(tmp_path: Path) -> None:
    outputs: list[tuple[bytes, bytes]] = []
    for hash_seed in ("1", "999983"):
        directory = tmp_path / hash_seed
        public_path = directory / "public.json"
        oracle_path = directory / "oracle.json"
        audit_path = directory / "audit.json"
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = hash_seed
        environment["PYTHONPATH"] = str(ROOT)
        subprocess.run(
            [
                sys.executable,
                str(GENERATOR),
                "--split",
                "smoke",
                "--public-output",
                str(public_path),
                "--oracle-output",
                str(oracle_path),
            ],
            check=True,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                sys.executable,
                str(VALIDATOR),
                "--public",
                str(public_path),
                "--oracle",
                str(oracle_path),
                "--report",
                str(audit_path),
                "--freeze-audit",
            ],
            check=True,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert load_strict_json(public_path)["instance_count"] == 40
        audit = load_strict_json(audit_path)["freeze_audit"]
        assert audit["input_file_sha256"]["public_manifest"]
        assert audit["input_file_sha256"]["oracle_manifest"]
        assert audit["freeze_file_sha256"]["protocol_freeze.json"]
        assert audit["code_sha256"]["coordcap/evaluation.py"]
        assert audit["code_sha256"]["coordcap/schema.py"]
        assert audit["credentials"] == {
            "credential_values_recorded": False,
            "environment_values_read": False,
        }
        outputs.append((public_path.read_bytes(), oracle_path.read_bytes()))
    assert outputs[0] == outputs[1]


def test_family_specs_are_new_finite_coordination_domains() -> None:
    assert set(FAMILY_SPECS) == set(TASK_FAMILIES)
    for family in TASK_FAMILIES:
        spec = FAMILY_SPECS[family]
        assert len(spec["issues"]) == 6
        assert all(len(values) == 4 for _issue, values in spec["issues"])
        assert len({issue for issue, _values in spec["issues"]}) == 6
        assert len(spec["resource_names"]) == 3
