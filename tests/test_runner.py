from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from coordcap.runner import (  # noqa: E402
    DEFAULT_BASE_URL,
    RunnerConfig,
    build_expected_run_ledger,
    canonical_sha256,
    request_sha256,
    run_coordcap,
)
from coordcap.schema import parse_and_validate, strict_json_loads  # noqa: E402


def public_task() -> dict[str, Any]:
    return {
        "task_family": "shared_plan_selection",
        "principals": [
            {
                "principal_id": "p1",
                "hard_constraints": ["must_include_a"],
                "soft_preferences": ["prefer_low_cost"],
                "priority_weight": 2,
            },
            {
                "principal_id": "p2",
                "hard_constraints": ["must_avoid_c"],
                "soft_preferences": ["prefer_fast"],
                "priority_weight": 1,
            },
        ],
        "plans": [
            {
                "plan_id": "plan_a",
                "decision": {
                    "settings": {"feature": "a"},
                    "resource_allocation": {"units": 1},
                },
            },
            {
                "plan_id": "plan_b",
                "decision": {
                    "settings": {"feature": "c"},
                    "resource_allocation": {"units": 2},
                },
            },
        ],
        "shared_constraints": {"select_exactly": 1},
    }


def write_manifest(path: Path, *, private_trap: bool = False) -> Path:
    task = public_task()
    instance: dict[str, Any] = {
        "instance_id": "coordcap_0000",
        "logical_id": "logical_0000",
        "task_family": "shared_plan_selection",
        "principal_count": 2,
        "conflict_level": "medium",
        "atomic_conflict_density_bp": 5000,
        "public_task": task,
        "public_sha256": canonical_sha256(task),
    }
    if private_trap:
        # The loader tolerates unrelated manifest metadata, but the runner must
        # never serialize it into a request.
        instance["gold_solution_private_trap"] = "NEVER_SEND_THIS_VALUE"
    manifest = {
        "protocol_version": "coordcap-v1",
        "split": "smoke",
        "master_seed": 20260717,
        "instance_count": 1,
        "instances": [instance],
    }
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def write_formal_manifest(path: Path) -> Path:
    families = (
        "resource_allocation",
        "scheduling",
        "shared_plan_selection",
        "policy_choice",
        "constrained_recommendation",
        "conflicting_information_requests",
    )
    instances: list[dict[str, Any]] = []
    index = 0
    for family in families:
        for principal_count in (2, 4, 6, 8):
            for conflict_level in ("low", "medium", "high"):
                for replicate in range(4):
                    task = {
                        "task_family": family,
                        "principals": [
                            {"principal_id": f"principal_{item + 1:02d}"}
                            for item in range(principal_count)
                        ],
                        "plans": [],
                        "shared_constraints": [],
                    }
                    instance_id = (
                        f"formal_{index:04d}_{family}_p{principal_count:02d}_"
                        f"{conflict_level}_r{replicate + 1:02d}"
                    )
                    instances.append(
                        {
                            "instance_id": instance_id,
                            "logical_id": f"coordcap_formal_{index:04d}",
                            "task_family": family,
                            "principal_count": principal_count,
                            "conflict_level": conflict_level,
                            "atomic_conflict_density_bp": 2000,
                            "public_task": task,
                            "public_sha256": canonical_sha256(task),
                        }
                    )
                    index += 1
    manifest = {
        "protocol_version": "coordcap-1.0.0",
        "split": "formal",
        "master_seed": 20260717,
        "instance_count": len(instances),
        "instances": instances,
    }
    path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_smoke_manifest(path: Path) -> Path:
    instances: list[dict[str, Any]] = []
    for index in range(40):
        task = public_task()
        instances.append(
            {
                "instance_id": f"smoke_{index:04d}_shared_plan_selection_p02_medium_r01",
                "logical_id": f"coordcap_smoke_{index:04d}",
                "task_family": "shared_plan_selection",
                "principal_count": 2,
                "conflict_level": "medium",
                "atomic_conflict_density_bp": 5000,
                "public_task": task,
                "public_sha256": canonical_sha256(task),
            }
        )
    manifest = {
        "protocol_version": "coordcap-1.0.0",
        "split": "smoke",
        "master_seed": 20260717,
        "instance_count": 40,
        "instances": instances,
    }
    path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    return path


def valid_final() -> dict[str, Any]:
    return {
        "selected_plan": {"plan_id": "plan_a"},
        "principal_outcomes": [
            {
                "principal_id": "p1",
                "satisfied_hard_constraints": ["must_include_a"],
                "violated_hard_constraints": [],
                "utility": None,
                "justification": "Plan a includes a.",
            },
            {
                "principal_id": "p2",
                "satisfied_hard_constraints": ["must_avoid_c"],
                "violated_hard_constraints": [],
                "utility": None,
                "justification": "Plan a avoids c.",
            },
        ],
        "resource_allocation": {"units": 1},
        "unresolved_conflicts": [],
        "abstain": False,
    }


class FakeTransport:
    def __init__(self, *, invalid_first_final: bool = False) -> None:
        self.invalid_first_final = invalid_first_final
        self.invalid_sent = False
        self.payloads: list[Mapping[str, Any]] = []

    async def request(
        self,
        *,
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        assert timeout_seconds > 0
        self.payloads.append(payload)
        schema_name = payload["response_format"]["json_schema"]["name"]
        if schema_name == "coordcap_analysis":
            content: Any = {
                "summary": "batch facts",
                "hard_constraints": ["one hard constraint"],
                "soft_preferences": ["one preference"],
                "conflicts": [],
                "candidate_actions": ["plan_a"],
                "uncertainties": [],
            }
        elif self.invalid_first_final and not self.invalid_sent:
            self.invalid_sent = True
            content = valid_final()
            content["principal_outcomes"][0]["utility"] = 0.75
        else:
            content = valid_final()
        completion_tokens = min(20, int(payload["max_tokens"]))
        return {
            "id": f"fake-{len(self.payloads)}",
            "model": payload["model"],
            "provider": "fake-provider",
            "choices": [
                {
                    "message": {
                        "content": json.dumps(content, ensure_ascii=False, separators=(",", ":"))
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 30,
                "completion_tokens": completion_tokens,
                "total_tokens": 30 + completion_tokens,
                "cost": 0.001,
            },
        }


class MissingCostTransport(FakeTransport):
    async def request(
        self,
        *,
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        response = dict(
            await super().request(payload=payload, timeout_seconds=timeout_seconds)
        )
        response["usage"] = dict(response["usage"])
        response["usage"].pop("cost")
        return response


class MissingUsageTransport(FakeTransport):
    def __init__(self, *, non_integer: bool = False) -> None:
        super().__init__()
        self.non_integer = non_integer

    async def request(
        self,
        *,
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        response = dict(
            await super().request(payload=payload, timeout_seconds=timeout_seconds)
        )
        response["usage"] = dict(response["usage"])
        if self.non_integer:
            response["usage"]["completion_tokens"] = "20"
        else:
            response["usage"].pop("completion_tokens")
        return response


class WrongResourceTransport(FakeTransport):
    async def request(
        self,
        *,
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        response = dict(
            await super().request(payload=payload, timeout_seconds=timeout_seconds)
        )
        choice = dict(response["choices"][0])
        message = dict(choice["message"])
        content = json.loads(message["content"])
        content["resource_allocation"] = {}
        message["content"] = json.dumps(content, separators=(",", ":"))
        choice["message"] = message
        response["choices"] = [choice]
        return response


class DecisionChangingRepairTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__(invalid_first_final=True)

    async def request(
        self,
        *,
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        response = dict(
            await super().request(payload=payload, timeout_seconds=timeout_seconds)
        )
        if payload["response_format"]["json_schema"]["name"] == "coordcap_final_repair":
            choice = dict(response["choices"][0])
            message = dict(choice["message"])
            content = json.loads(message["content"])
            content["selected_plan"] = {"plan_id": "plan_b"}
            content["resource_allocation"] = {"units": 2}
            message["content"] = json.dumps(content, separators=(",", ":"))
            choice["message"] = message
            response["choices"] = [choice]
        return response


class UnparseableFirstTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.first_final_sent = False

    async def request(
        self,
        *,
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        response = dict(
            await super().request(payload=payload, timeout_seconds=timeout_seconds)
        )
        schema_name = payload["response_format"]["json_schema"]["name"]
        if schema_name == "coordcap_final" and not self.first_final_sent:
            self.first_final_sent = True
            choice = dict(response["choices"][0])
            message = dict(choice["message"])
            message["content"] = "not JSON"
            choice["message"] = message
            response["choices"] = [choice]
        return response


class RouteSubstitutionTransport(FakeTransport):
    async def request(
        self,
        *,
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        response = dict(
            await super().request(payload=payload, timeout_seconds=timeout_seconds)
        )
        response["model"] = "substituted/provider-route"
        return response


class RaisingTransport:
    def __init__(self) -> None:
        self.calls = 0

    async def request(
        self,
        *,
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        self.calls += 1
        raise TimeoutError("synthetic timeout")


def config_for(
    root: Path,
    manifest: Path,
    *,
    method: str,
    calls: int,
    tokens: int = 512,
    repair: bool = True,
    order_variants: tuple[str, ...] = ("canonical",),
) -> RunnerConfig:
    return RunnerConfig(
        public_manifest_path=manifest,
        models=("fake/coord-model",),
        methods=(method,),
        max_answer_tokens=(tokens,),
        max_model_calls=(calls,),
        order_variants=order_variants,
        raw_root=root / "raw",
        parsed_root=root / "parsed",
        cache_root=root / "cache",
        audit_log=root / "raw" / "api_calls.jsonl",
        expected_ledger_path=root / "expected_runs.json",
        concurrency=3,
        repair_invalid=repair,
    )


def one_terminal(root: Path) -> dict[str, Any]:
    paths = list((root / "parsed").glob("*.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


def test_strict_parser_rejects_duplicate_keys_nonfinite_and_extra_plan_fields() -> None:
    value, error = strict_json_loads('{"a":1,"a":2}')
    assert value is None
    assert "duplicate object key" in str(error)
    value, error = strict_json_loads('{"a":NaN}')
    assert value is None
    assert "non-finite" in str(error)
    candidate = valid_final()
    candidate["selected_plan"]["unfrozen_explanation"] = "not allowed"
    _, valid, errors = parse_and_validate(json.dumps(candidate), final=True)
    assert not valid
    assert any("Additional properties" in error for error in errors)


def test_expected_ledger_exact_shape_and_deterministic_run_ids(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "public.json")
    config = config_for(
        tmp_path,
        manifest,
        method="direct_joint_prompt",
        calls=1,
        order_variants=("canonical", "reverse"),
    )
    first = build_expected_run_ledger(config)
    second = build_expected_run_ledger(config)
    assert first == second
    assert set(first) == {
        "protocol_version",
        "public_manifest_sha256",
        "execution_config_sha256",
        "runs",
    }
    assert first["execution_config_sha256"]
    assert len(first["runs"]) == 2
    assert set(first["runs"][0]) == {
        "run_id",
        "instance_id",
        "order_variant",
        "model",
        "method",
        "max_answer_tokens",
        "max_model_calls",
    }
    assert first["runs"][0]["run_id"] != first["runs"][1]["run_id"]


def test_frozen_formal_ledger_has_primary_plus_only_the_192_reverse_panel(
    tmp_path: Path,
) -> None:
    manifest = write_formal_manifest(tmp_path / "formal_public.json")
    config = RunnerConfig(
        public_manifest_path=manifest,
        models=("google/gemini-3.1-flash-lite", "openai/gpt-5.4-mini"),
        include_frozen_reverse_panel=True,
        raw_root=tmp_path / "raw",
        parsed_root=tmp_path / "parsed",
        cache_root=tmp_path / "cache",
        expected_ledger_path=tmp_path / "expected.json",
    )
    ledger = build_expected_run_ledger(config)
    variants = Counter(run["order_variant"] for run in ledger["runs"])
    assert variants == {"canonical": 20_736, "reverse": 192}
    reverse = [run for run in ledger["runs"] if run["order_variant"] == "reverse"]
    assert {(run["max_answer_tokens"], run["max_model_calls"]) for run in reverse} == {
        (1024, 2)
    }
    assert len({run["instance_id"] for run in reverse}) == 24
    assert all(run["instance_id"].endswith("_r01") for run in reverse)
    assert len({run["run_id"] for run in ledger["runs"]}) == 20_928


def test_frozen_smoke_ledger_is_exactly_320_and_execution_drift_fails_closed(
    tmp_path: Path,
) -> None:
    manifest = write_smoke_manifest(tmp_path / "smoke_public.json")
    common: dict[str, Any] = {
        "public_manifest_path": manifest,
        "models": ("google/gemini-3.1-flash-lite", "openai/gpt-5.4-mini"),
        "methods": (
            "direct_joint_prompt",
            "sequential_aggregation",
            "constraint_ledger",
            "budget_aware_planner",
        ),
        "max_answer_tokens": (512,),
        "max_model_calls": (2,),
        "order_variants": ("canonical",),
        "enforce_frozen_smoke": True,
        "raw_root": tmp_path / "raw",
        "parsed_root": tmp_path / "parsed",
        "cache_root": tmp_path / "cache",
        "expected_ledger_path": tmp_path / "expected.json",
        "concurrency": 8,
    }
    ledger = build_expected_run_ledger(RunnerConfig(**common))
    assert len(ledger["runs"]) == 320
    drift_cases = (
        {"temperature": 0.1},
        {"api_seed": 1},
        {"repair_invalid": False},
        {"base_url": "https://example.test/v1/chat/completions"},
        {"use_cache": False},
        {"resume": False},
        {"concurrency": 7},
        {"timeout_seconds": 179.0},
    )
    for drift in drift_cases:
        with pytest.raises(ValueError, match="frozen smoke"):
            build_expected_run_ledger(RunnerConfig(**{**common, **drift}))
    assert RunnerConfig(**common).base_url == DEFAULT_BASE_URL


def test_four_method_call_plans_and_sequential_batching(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "public.json", private_trap=True)
    expected_counts = {
        "direct_joint_prompt": (4, 1),
        # Two principals cap c-1 analysis stages at two: no empty batch call.
        "sequential_aggregation": (4, 3),
        "constraint_ledger": (2, 2),
        "budget_aware_planner": (4, 4),
    }
    for method, (budget, expected_calls) in expected_counts.items():
        method_root = tmp_path / method
        fake = FakeTransport()
        summary = asyncio.run(
            run_coordcap(
                config_for(method_root, manifest, method=method, calls=budget),
                transport=fake,
            )
        )
        assert summary["status_counts"] == {"complete": 1}
        assert len(fake.payloads) == expected_calls
        terminal = one_terminal(method_root)
        assert terminal["call_count"] == expected_calls
        assert terminal["requested_completion_token_cap"] <= 512
        assert terminal["total_completion_tokens"] <= 512
        serialized_payloads = json.dumps(fake.payloads, ensure_ascii=False)
        assert "NEVER_SEND_THIS_VALUE" not in serialized_payloads

        if method == "sequential_aggregation":
            first_batch = fake.payloads[0]["messages"][-1]["content"]
            second_batch = fake.payloads[1]["messages"][-1]["content"]
            final_prompt = fake.payloads[-1]["messages"][-1]["content"]
            assert '"principal_id":"p1"' in first_batch
            assert '"principal_id":"p2"' not in first_batch
            assert '"principal_id":"p2"' in second_batch
            assert '"principal_id":"p1"' not in second_batch
            assert '"principals":' not in final_prompt
            assert "principal_ids_in_order" in final_prompt


def test_one_repair_counts_against_call_and_token_budgets(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "public.json")
    fake = FakeTransport(invalid_first_final=True)
    config = config_for(
        tmp_path,
        manifest,
        method="direct_joint_prompt",
        calls=2,
        tokens=512,
    )
    summary = asyncio.run(run_coordcap(config, transport=fake))
    assert summary["status_counts"] == {"complete": 1}
    terminal = one_terminal(tmp_path)
    assert terminal["initial_json_valid"] is False
    assert terminal["effective_json_valid"] is True
    assert terminal["repair_used"] is True
    assert terminal["call_count"] == 2
    assert terminal["requested_completion_token_cap"] == 512
    assert terminal["sum_call_max_tokens"] == 1004
    assert terminal["total_completion_tokens"] <= 512
    assert terminal["parsed_output"]["principal_outcomes"][0]["utility"] is None
    assert terminal["repair_decision_preservation_verifiable"] is True
    assert terminal["repair_decision_changed"] is False
    assert [call["stage"] for call in terminal["calls"]] == ["final", "repair_final"]


def test_schema_repair_cannot_change_selected_plan(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "public.json")
    fake = DecisionChangingRepairTransport()
    summary = asyncio.run(
        run_coordcap(
            config_for(tmp_path, manifest, method="direct_joint_prompt", calls=2),
            transport=fake,
        )
    )
    assert summary["status_counts"] == {"invalid_output": 1}
    terminal = one_terminal(tmp_path)
    assert terminal["repair_used"] is True
    assert terminal["repair_decision_preservation_verifiable"] is True
    assert terminal["repair_decision_changed"] is True
    assert terminal["effective_json_valid"] is False
    assert "repair_changed_selected_plan" in terminal["effective_validation_errors"]


def test_unparseable_initial_repair_marks_decision_preservation_unverifiable(
    tmp_path: Path,
) -> None:
    manifest = write_manifest(tmp_path / "public.json")
    fake = UnparseableFirstTransport()
    summary = asyncio.run(
        run_coordcap(
            config_for(tmp_path, manifest, method="direct_joint_prompt", calls=2),
            transport=fake,
        )
    )
    assert summary["status_counts"] == {"complete": 1}
    terminal = one_terminal(tmp_path)
    assert terminal["repair_used"] is True
    assert terminal["repair_decision_preservation_verifiable"] is False
    assert terminal["repair_decision_changed"] is False


def test_invalid_without_budget_for_repair_is_retained(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "public.json")
    fake = FakeTransport(invalid_first_final=True)
    config = config_for(
        tmp_path,
        manifest,
        method="direct_joint_prompt",
        calls=1,
        repair=True,
    )
    summary = asyncio.run(run_coordcap(config, transport=fake))
    assert summary["status_counts"] == {"invalid_output": 1}
    terminal = one_terminal(tmp_path)
    assert terminal["repair_used"] is False
    assert terminal["effective_json_valid"] is False
    assert terminal["parsed_output"]["principal_outcomes"][0]["utility"] == 0.75
    assert len(terminal["raw_paths"]) == 1


def test_local_validator_uses_the_same_closed_resource_schema_as_provider(
    tmp_path: Path,
) -> None:
    manifest = write_manifest(tmp_path / "public.json")
    fake = WrongResourceTransport()
    summary = asyncio.run(
        run_coordcap(
            config_for(tmp_path, manifest, method="direct_joint_prompt", calls=1),
            transport=fake,
        )
    )
    assert summary["status_counts"] == {"invalid_output": 1}
    terminal = one_terminal(tmp_path)
    assert terminal["initial_json_valid"] is False
    assert any(
        "'units' is a required property" in error
        for error in terminal["initial_validation_errors"]
    )


def test_missing_provider_cost_is_null_not_fabricated_zero(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "public.json")
    fake = MissingCostTransport()
    summary = asyncio.run(
        run_coordcap(
            config_for(tmp_path, manifest, method="direct_joint_prompt", calls=1),
            transport=fake,
        )
    )
    assert summary["status_counts"] == {"complete": 1}
    terminal = one_terminal(tmp_path)
    assert terminal["reported_cost"] is None
    assert terminal["reported_cost_partial"] == 0.0
    assert terminal["reported_cost_complete"] is False
    assert terminal["calls"][0]["returned_provider"] == "fake-provider"


def test_route_substitution_is_terminal_and_cannot_be_complete(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "public.json")
    summary = asyncio.run(
        run_coordcap(
            config_for(tmp_path, manifest, method="direct_joint_prompt", calls=1),
            transport=RouteSubstitutionTransport(),
        )
    )
    assert summary["status_counts"] == {"route_mismatch": 1}
    terminal = one_terminal(tmp_path)
    assert terminal["route_consistency_rule"] == (
        "exact_returned_model_equals_requested_model"
    )
    assert terminal["route_consistent"] is False
    assert terminal["calls"][0]["route_consistent"] is False
    assert terminal["calls"][0]["returned_model"] == "substituted/provider-route"
    assert terminal["usage_complete"] is True
    assert terminal["budget_compliant"] is True


@pytest.mark.parametrize("non_integer", [False, True])
def test_missing_or_noninteger_usage_cannot_be_complete_or_budget_compliant(
    tmp_path: Path,
    non_integer: bool,
) -> None:
    manifest = write_manifest(tmp_path / "public.json")
    fake = MissingUsageTransport(non_integer=non_integer)
    summary = asyncio.run(
        run_coordcap(
            config_for(tmp_path, manifest, method="direct_joint_prompt", calls=1),
            transport=fake,
        )
    )
    assert summary["status_counts"] == {"usage_unavailable": 1}
    terminal = one_terminal(tmp_path)
    assert terminal["usage_complete"] is False
    assert terminal["budget_compliant"] is False
    assert terminal["total_prompt_tokens"] is None
    assert terminal["total_completion_tokens"] is None
    assert terminal["calls"][0]["usage_complete"] is False


def test_transport_failure_still_writes_one_terminal_denominator_row(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "public.json")
    transport = RaisingTransport()
    summary = asyncio.run(
        run_coordcap(
            config_for(tmp_path, manifest, method="direct_joint_prompt", calls=2),
            transport=transport,
        )
    )
    assert summary["expected_runs"] == summary["terminal_records"] == 1
    assert summary["status_counts"] == {"usage_unavailable": 1}
    terminal = one_terminal(tmp_path)
    assert terminal["initial_json_valid"] is False
    assert terminal["repair_used"] is False  # usage missing, so remaining budget is unknowable
    assert terminal["reported_cost"] is None
    assert terminal["usage_complete"] is False
    assert len(terminal["raw_paths"]) == 1


def test_frozen_model_sampling_parameters_and_required_parameter_routing(
    tmp_path: Path,
) -> None:
    manifest = write_manifest(tmp_path / "public.json")
    fake = FakeTransport()
    config = RunnerConfig(
        public_manifest_path=manifest,
        models=("google/gemini-3.1-flash-lite", "openai/gpt-5.4-mini"),
        methods=("direct_joint_prompt",),
        max_answer_tokens=(512,),
        max_model_calls=(1,),
        raw_root=tmp_path / "raw",
        parsed_root=tmp_path / "parsed",
        cache_root=tmp_path / "cache",
        expected_ledger_path=tmp_path / "expected.json",
    )
    summary = asyncio.run(run_coordcap(config, transport=fake))
    assert summary["status_counts"] == {"complete": 2}
    by_model = {payload["model"]: payload for payload in fake.payloads}
    gemini = by_model["google/gemini-3.1-flash-lite"]
    gpt = by_model["openai/gpt-5.4-mini"]
    assert gemini["temperature"] == 0.0 and gemini["seed"] == 0
    assert "temperature" not in gpt and gpt["seed"] == 0
    assert gemini["provider"] == gpt["provider"] == {"require_parameters": True}
    resource_schema = gemini["response_format"]["json_schema"]["schema"]["properties"][
        "resource_allocation"
    ]
    assert resource_schema == {
        "type": "object",
        "additionalProperties": False,
        "required": ["units"],
        "properties": {"units": {"type": "integer"}},
    }


def test_immutable_raw_resume_and_parsed_resume_make_no_network_call(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "public.json")
    config = config_for(tmp_path, manifest, method="direct_joint_prompt", calls=1)
    first_fake = FakeTransport()
    first = asyncio.run(run_coordcap(config, transport=first_fake))
    assert len(first_fake.payloads) == 1
    terminal = one_terminal(tmp_path)
    ledger = json.loads(config.expected_ledger_path.read_text(encoding="utf-8"))
    assert (
        first["execution_config_sha256"]
        == ledger["execution_config_sha256"]
        == terminal["execution_config_sha256"]
    )
    raw_path = Path(terminal["raw_paths"][0])
    raw_bytes = raw_path.read_bytes()
    raw_record = json.loads(raw_bytes)
    assert raw_record["response_payload_sha256"]

    parsed_resume_fake = FakeTransport()
    second = asyncio.run(run_coordcap(config, transport=parsed_resume_fake))
    assert second["resumed_records"] == 1
    assert parsed_resume_fake.payloads == []
    assert raw_path.read_bytes() == raw_bytes

    # Simulate a crash after immutable raw creation but before terminal write.
    parsed_path = next((tmp_path / "parsed").glob("*.json"))
    parsed_path.unlink()
    raw_resume_fake = FakeTransport()
    third = asyncio.run(run_coordcap(config, transport=raw_resume_fake))
    assert third["status_counts"] == {"complete": 1}
    assert raw_resume_fake.payloads == []
    terminal = one_terminal(tmp_path)
    assert terminal["calls"][0]["source"] == "raw_resume"
    assert raw_path.read_bytes() == raw_bytes


def test_parsed_resume_revalidates_raw_response_integrity(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "public.json")
    config = config_for(tmp_path, manifest, method="direct_joint_prompt", calls=1)
    asyncio.run(run_coordcap(config, transport=FakeTransport()))
    raw_path = Path(one_terminal(tmp_path)["raw_paths"][0])
    tampered = json.loads(raw_path.read_text(encoding="utf-8"))
    tampered["response_payload"]["model"] = "tampered/model"
    raw_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(RuntimeError, match="raw response payload hash mismatch"):
        asyncio.run(run_coordcap(config, transport=FakeTransport()))


def test_raw_resume_rejects_tampered_request_payload(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "public.json")
    config = config_for(tmp_path, manifest, method="direct_joint_prompt", calls=1)
    asyncio.run(run_coordcap(config, transport=FakeTransport()))
    terminal = one_terminal(tmp_path)
    raw_path = Path(terminal["raw_paths"][0])
    next((tmp_path / "parsed").glob("*.json")).unlink()
    tampered = json.loads(raw_path.read_text(encoding="utf-8"))
    tampered["request_payload"]["max_tokens"] = 1
    raw_path.write_text(json.dumps(tampered), encoding="utf-8")
    summary = asyncio.run(run_coordcap(config, transport=FakeTransport()))
    assert summary["status_counts"] == {"runner_error": 1}
    assert "raw request payload hash mismatch" in one_terminal(tmp_path)["error"]


def test_cache_hit_rejects_tampered_response_payload(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "public.json")
    config = config_for(tmp_path, manifest, method="direct_joint_prompt", calls=1)
    asyncio.run(run_coordcap(config, transport=FakeTransport()))
    terminal = one_terminal(tmp_path)
    Path(terminal["raw_paths"][0]).unlink()
    next((tmp_path / "parsed").glob("*.json")).unlink()
    cache_path = next((tmp_path / "cache").glob("*.json"))
    tampered = json.loads(cache_path.read_text(encoding="utf-8"))
    tampered["response_payload"]["model"] = "tampered/model"
    cache_path.write_text(json.dumps(tampered), encoding="utf-8")
    summary = asyncio.run(run_coordcap(config, transport=FakeTransport()))
    assert summary["status_counts"] == {"runner_error": 1}
    assert "cache response payload hash mismatch" in one_terminal(tmp_path)["error"]


def test_request_hash_is_canonical() -> None:
    first = request_sha256(base_url="https://example.test/v1", payload={"b": 2, "a": 1})
    second = request_sha256(base_url="https://example.test/v1", payload={"a": 1, "b": 2})
    assert first == second
