"""Expected-ledger-first evaluation for CoordCap.

The expected-run ledger, never the set of discovered output files, defines the
denominator.  Exactly one scored row is emitted for every frozen run ID.
Missing, duplicate, malformed, abstaining, unknown, infeasible, and
budget-invalid attempts therefore cannot disappear during evaluation.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .statistics import (
    BOOTSTRAP_SAMPLES,
    BOOTSTRAP_SEED,
    capacity_analysis,
    metric_cells,
)


PROTOCOL_VERSION = "coordcap-1.0.0"
UTILITY_SCALE = 1000
SUCCESS_WELFARE_GAP_MAX = 0.05

FROZEN_MASTER_SEED = 20260717
FROZEN_MODELS = ("google/gemini-3.1-flash-lite", "openai/gpt-5.4-mini")
FROZEN_METHODS = (
    "direct_joint_prompt",
    "sequential_aggregation",
    "constraint_ledger",
    "budget_aware_planner",
)
FROZEN_TOKEN_BUDGETS = (512, 1024, 2048)
FROZEN_CALL_BUDGETS = (1, 2, 4)
FROZEN_PRINCIPAL_COUNTS = (2, 4, 6, 8)
FROZEN_CONFLICT_LEVELS = ("low", "medium", "high")
FROZEN_TASK_FAMILIES = (
    "resource_allocation",
    "scheduling",
    "shared_plan_selection",
    "policy_choice",
    "constrained_recommendation",
    "conflicting_information_requests",
)
FORMAL_INSTANCE_COUNT = 288
FORMAL_CANONICAL_RUNS = 20_736
FORMAL_REVERSE_RUNS = 192
FORMAL_TOTAL_RUNS = FORMAL_CANONICAL_RUNS + FORMAL_REVERSE_RUNS
FORMAL_CONSISTENCY_INSTANCES = 24
SMOKE_INSTANCE_COUNT = 40
SMOKE_TOTAL_RUNS = 320

RUN_IDENTITY_FIELDS = (
    "run_id",
    "instance_id",
    "order_variant",
    "model",
    "method",
    "max_answer_tokens",
    "max_model_calls",
)

NONTERMINAL_STATUSES = {"", "pending", "scheduled", "queued", "running", "in_progress"}


class EvaluationError(ValueError):
    """Raised when frozen manifests or oracle material are internally invalid."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256((canonical_json(value) + "\n").encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _object_without_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise EvaluationError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _reject_nonfinite(value: str) -> Any:
    raise EvaluationError(f"non-finite JSON number {value!r}")


def _strict_json(text: str, source: str) -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (json.JSONDecodeError, EvaluationError, ValueError) as error:
        raise EvaluationError(f"{source}: invalid JSON: {error}") from error


def load_json(path: str | Path) -> Any:
    source = Path(path)
    return _strict_json(source.read_text(encoding="utf-8"), str(source))


def _records_from_document(document: Any) -> list[dict[str, Any]]:
    if isinstance(document, list):
        return [dict(item) for item in document if isinstance(item, Mapping)]
    if not isinstance(document, Mapping):
        return []
    for key in ("records", "runs", "terminals", "outputs"):
        value = document.get(key)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, Mapping)]
    return [dict(document)] if "run_id" in document else []


def read_parsed_records(path: str | Path | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read JSON/JSONL terminals deterministically, preserving unattributed errors."""

    if path is None:
        return [], []
    root = Path(path)
    files = [root] if root.is_file() else sorted(root.rglob("*.json")) + sorted(root.rglob("*.jsonl"))
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for source in files:
        if source.suffix.lower() == ".jsonl":
            for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    document = _strict_json(line, f"{source}:{line_number}")
                except EvaluationError as error:
                    errors.append({"path": str(source), "line": line_number, "error": str(error)})
                    continue
                for record in _records_from_document(document):
                    record["_source_path"] = str(source)
                    record["_source_line"] = line_number
                    records.append(record)
        else:
            try:
                document = _strict_json(source.read_text(encoding="utf-8"), str(source))
            except EvaluationError as error:
                errors.append({"path": str(source), "line": None, "error": str(error)})
                continue
            for record in _records_from_document(document):
                record["_source_path"] = str(source)
                records.append(record)
    return records, errors


def _as_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EvaluationError(f"{label} must be an object")
    return dict(value)


def _id_from(item: Mapping[str, Any], names: Sequence[str], label: str) -> str:
    for name in names:
        value = item.get(name)
        if isinstance(value, str) and value:
            return value
    raise EvaluationError(f"{label} has no stable ID")


def _table_by_plan(value: Any, *, value_keys: Sequence[str]) -> dict[str, dict[str, int]]:
    output: dict[str, dict[str, int]] = {}
    if isinstance(value, Mapping):
        iterable = value.items()
    elif isinstance(value, list):
        iterable = []
        for item in value:
            if not isinstance(item, Mapping):
                continue
            plan_id = item.get("plan_id") or item.get("id")
            if isinstance(plan_id, str):
                payload = next((item.get(key) for key in value_keys if key in item), None)
                iterable.append((plan_id, payload))
    else:
        iterable = []
    for plan_id, payload in iterable:
        if not isinstance(payload, Mapping):
            raise EvaluationError(f"oracle utility row {plan_id!r} must be an object")
        row: dict[str, int] = {}
        for key, item in payload.items():
            if isinstance(item, bool) or not isinstance(item, int):
                raise EvaluationError(
                    f"oracle utility {plan_id!r}/{key!r} must be an integer"
                )
            row[str(key)] = item
        output[str(plan_id)] = row
    return output


def _list_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(sorted(str(item) for item in value))


def _pareto_ids(feasible: Iterable[str], utilities: Mapping[str, Mapping[str, int]], principals: Sequence[str]) -> tuple[str, ...]:
    feasible_ids = sorted(set(feasible))
    frontier: list[str] = []
    for candidate in feasible_ids:
        candidate_vector = utilities[candidate]
        dominated = False
        for other in feasible_ids:
            if other == candidate:
                continue
            other_vector = utilities[other]
            weak = all(other_vector[pid] >= candidate_vector[pid] for pid in principals)
            strict = any(other_vector[pid] > candidate_vector[pid] for pid in principals)
            if weak and strict:
                dominated = True
                break
        if not dominated:
            frontier.append(candidate)
    return tuple(frontier)


@dataclass(frozen=True)
class OracleView:
    principal_ids: tuple[str, ...]
    priority_weights: dict[str, int]
    plans: dict[str, dict[str, Any]]
    feasible_plan_ids: frozenset[str]
    utilities: dict[str, dict[str, int]]
    ideal_utilities: dict[str, int]
    weighted_welfare: dict[str, int]
    pareto_plan_ids: frozenset[str]
    pareto_utility_vectors: tuple[tuple[int, ...], ...]
    representative_plan_id: str
    violations: dict[str, dict[str, tuple[str, ...]]]

    @property
    def maximum_weighted_welfare(self) -> int:
        return max(self.weighted_welfare[plan_id] for plan_id in self.feasible_plan_ids)


def normalize_oracle(public_task: Mapping[str, Any], oracle: Mapping[str, Any]) -> OracleView:
    """Normalize and independently verify the stored integer oracle tables."""

    principals_raw = public_task.get("principals")
    if not isinstance(principals_raw, list) or not principals_raw:
        raise EvaluationError("public_task.principals must be a non-empty array")
    principal_ids: list[str] = []
    weights: dict[str, int] = {}
    for index, principal in enumerate(principals_raw):
        item = _as_dict(principal, f"principal[{index}]")
        principal_id = _id_from(item, ("principal_id", "id"), f"principal[{index}]")
        weight = item.get("priority_weight", item.get("weight"))
        if isinstance(weight, bool) or not isinstance(weight, int) or weight < 1:
            raise EvaluationError(f"principal {principal_id} has invalid priority weight")
        if principal_id in weights:
            raise EvaluationError(f"duplicate principal ID {principal_id}")
        principal_ids.append(principal_id)
        weights[principal_id] = weight
    principal_tuple = tuple(sorted(principal_ids))

    catalogue = next(
        (
            public_task.get(key)
            for key in ("candidate_plans", "plans", "candidate_catalogue", "plan_catalogue")
            if key in public_task
        ),
        None,
    )
    plans: dict[str, dict[str, Any]] = {}
    if isinstance(catalogue, Mapping):
        for plan_id, value in catalogue.items():
            plan = dict(value) if isinstance(value, Mapping) else {"value": value}
            plan.setdefault("plan_id", str(plan_id))
            plans[str(plan_id)] = plan
    elif isinstance(catalogue, list):
        for index, value in enumerate(catalogue):
            plan = _as_dict(value, f"candidate_plans[{index}]")
            plan_id = _id_from(plan, ("plan_id", "id"), f"candidate_plans[{index}]")
            if plan_id in plans:
                raise EvaluationError(f"duplicate plan ID {plan_id}")
            plans[plan_id] = plan
    if not plans:
        raise EvaluationError("public task has no finite candidate-plan catalogue")

    oracle_obj = _as_dict(oracle, "oracle")
    declared_principals = oracle_obj.get("principal_ids")
    if (
        not isinstance(declared_principals, list)
        or any(not isinstance(value, str) for value in declared_principals)
        or tuple(sorted(declared_principals)) != principal_tuple
    ):
        raise EvaluationError("oracle principal_ids disagree with the public task")
    declared_weights = oracle_obj.get("priority_weights")
    if not isinstance(declared_weights, Mapping):
        raise EvaluationError("oracle priority_weights must be an object")
    normalized_weights: dict[str, int] = {}
    for key, value in declared_weights.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise EvaluationError("oracle priority_weights must contain integers")
        normalized_weights[str(key)] = value
    if normalized_weights != weights:
        raise EvaluationError("oracle priority_weights disagree with the public task")
    declared_domain = oracle_obj.get("domain_plan_ids")
    if declared_domain is not None:
        if (
            not isinstance(declared_domain, list)
            or any(not isinstance(value, str) for value in declared_domain)
            or set(declared_domain) != set(plans)
            or len(declared_domain) != len(plans)
        ):
            raise EvaluationError("oracle domain_plan_ids disagree with the public catalogue")

    evaluations = oracle_obj.get("plan_evaluations")
    evaluation_map: dict[str, Mapping[str, Any]] = {}
    if isinstance(evaluations, Mapping):
        for key, value in evaluations.items():
            if not isinstance(value, Mapping):
                raise EvaluationError(f"plan_evaluations[{key!r}] must be an object")
            evaluation_map[str(key)] = value
    elif isinstance(evaluations, list):
        for item in evaluations:
            if isinstance(item, Mapping) and isinstance(item.get("plan_id"), str):
                evaluation_map[str(item["plan_id"])] = item
    if evaluation_map and set(evaluation_map) != set(plans):
        raise EvaluationError("oracle plan_evaluations do not cover the public catalogue")

    utility_source = next(
        (
            oracle_obj.get(key)
            for key in (
                "utility_table",
                "utilities",
                "plan_utilities",
                "utility_bp_by_plan",
            )
            if key in oracle_obj
        ),
        None,
    )
    utilities = _table_by_plan(utility_source, value_keys=("utilities", "utility"))
    for plan_id, evaluation in evaluation_map.items():
        payload = evaluation.get(
            "utilities", evaluation.get("utility_bp", evaluation.get("utility"))
        )
        if isinstance(payload, Mapping):
            row: dict[str, int] = {}
            for key, value in payload.items():
                if isinstance(value, bool) or not isinstance(value, int):
                    raise EvaluationError(
                        f"plan_evaluations[{plan_id!r}] utility values must be integers"
                    )
                row[str(key)] = value
            if plan_id in utilities and utilities[plan_id] != row:
                raise EvaluationError(
                    f"plan_evaluations[{plan_id!r}] utility disagrees with utility table"
                )
            utilities.setdefault(plan_id, row)
    if set(utilities) != set(plans):
        raise EvaluationError("oracle utility table does not cover the public catalogue")

    feasible_value = next(
        (
            oracle_obj.get(key)
            for key in (
                "feasible_plan_ids",
                "joint_feasible_plan_ids",
                "feasible_plans",
                "feasible_set",
            )
            if key in oracle_obj
        ),
        None,
    )
    feasible = set(_list_ids(feasible_value))
    if not feasible and evaluation_map:
        feasible = {
            plan_id for plan_id, value in evaluation_map.items() if value.get("feasible") is True
        }
    if not feasible or not feasible <= set(plans):
        raise EvaluationError("oracle feasible set is empty or references unknown plans")

    for alias in ("feasible_plan_ids", "joint_feasible_plan_ids"):
        declared = oracle_obj.get(alias)
        if declared is not None and set(_list_ids(declared)) != feasible:
            raise EvaluationError(f"oracle {alias} disagrees with the feasible set")
    if evaluation_map:
        evaluation_feasible = {
            plan_id for plan_id, value in evaluation_map.items() if value.get("feasible") is True
        }
        if evaluation_feasible != feasible:
            raise EvaluationError("plan_evaluations feasibility disagrees with the feasible set")

    for plan_id in plans:
        if plan_id not in utilities or set(utilities[plan_id]) != set(principal_tuple):
            raise EvaluationError(f"oracle utility table is incomplete for {plan_id}")
        for principal_id, value in utilities[plan_id].items():
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= UTILITY_SCALE:
                raise EvaluationError(
                    f"utility {plan_id}/{principal_id} must be an integer in [0,1000]"
                )

    ideal = {
        principal_id: max(utilities[plan_id][principal_id] for plan_id in feasible)
        for principal_id in principal_tuple
    }
    for ideal_key in ("ideal_utilities", "ideal_bp"):
        declared_ideal = oracle_obj.get(ideal_key)
        if declared_ideal is None:
            continue
        if not isinstance(declared_ideal, Mapping):
            raise EvaluationError(f"declared {ideal_key} must be an object")
        normalized_ideal: dict[str, int] = {}
        for key, value in declared_ideal.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise EvaluationError("declared ideal utilities must be integers")
            normalized_ideal[str(key)] = value
        if normalized_ideal != ideal:
            raise EvaluationError("declared ideal utilities disagree with the feasible utility table")

    welfare = {
        plan_id: sum(weights[principal_id] * utilities[plan_id][principal_id] for principal_id in principal_tuple)
        for plan_id in plans
    }
    for welfare_key in ("weighted_welfare", "weighted_welfare_by_plan"):
        declared_welfare = oracle_obj.get(welfare_key)
        if declared_welfare is None:
            continue
        if not isinstance(declared_welfare, Mapping):
            raise EvaluationError(f"declared {welfare_key} must be an object")
        comparable: dict[str, int] = {}
        for key, value in declared_welfare.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise EvaluationError("declared weighted welfare must contain integers")
            comparable[str(key)] = value
        if comparable != welfare:
            raise EvaluationError("declared weighted welfare disagrees with recomputation")

    expected_regrets = {
        plan_id: {
            principal_id: max(0, ideal[principal_id] - utilities[plan_id][principal_id])
            for principal_id in principal_tuple
        }
        for plan_id in plans
    }
    declared_regrets = oracle_obj.get("regret_bp_by_plan")
    if declared_regrets is not None:
        actual_regrets = _table_by_plan(
            declared_regrets, value_keys=("regret_bp", "regrets")
        )
        if actual_regrets != expected_regrets:
            raise EvaluationError("declared regret table disagrees with recomputation")

    pareto = set(_pareto_ids(feasible, utilities, principal_tuple))
    declared_pareto = oracle_obj.get("pareto_plan_ids")
    if declared_pareto is None or set(_list_ids(declared_pareto)) != pareto:
        raise EvaluationError("declared Pareto plan IDs disagree with recomputation")
    pareto_vectors = tuple(
        sorted(
            {
                tuple(utilities[plan_id][principal_id] for principal_id in principal_tuple)
                for plan_id in pareto
            }
        )
    )
    declared_vectors = oracle_obj.get("pareto_utility_vectors")
    if not isinstance(declared_vectors, list):
        raise EvaluationError("oracle pareto_utility_vectors must be an array")
    normalized_vectors: list[tuple[int, ...]] = []
    for vector in declared_vectors:
        if (
            not isinstance(vector, list)
            or len(vector) != len(principal_tuple)
            or any(isinstance(value, bool) or not isinstance(value, int) for value in vector)
        ):
            raise EvaluationError("oracle pareto_utility_vectors contains an invalid vector")
        normalized_vectors.append(tuple(vector))
    if tuple(normalized_vectors) != pareto_vectors:
        raise EvaluationError("declared Pareto utility vectors disagree with recomputation")

    representative = min(
        feasible,
        key=lambda plan_id: (
            -welfare[plan_id],
            max(expected_regrets[plan_id].values()),
            canonical_json(plans[plan_id]),
        ),
    )
    for representative_key in ("representative_plan_id", "gold_plan_id"):
        if oracle_obj.get(representative_key) != representative:
            raise EvaluationError(
                f"oracle {representative_key} disagrees with representative recomputation"
            )
    for representative_welfare_key in (
        "representative_weighted_welfare",
        "gold_weighted_welfare",
    ):
        declared = oracle_obj.get(representative_welfare_key)
        if declared is not None and declared != welfare[representative]:
            raise EvaluationError(
                f"oracle {representative_welfare_key} disagrees with representative recomputation"
            )

    violations: dict[str, dict[str, tuple[str, ...]]] = {}
    for plan_id, evaluation in evaluation_map.items():
        raw = evaluation.get("violated_hard_constraints") or evaluation.get("violations")
        if isinstance(raw, Mapping):
            violations[plan_id] = {
                str(principal_id): tuple(sorted(str(value) for value in values))
                for principal_id, values in raw.items()
                if isinstance(values, list)
            }

    return OracleView(
        principal_ids=principal_tuple,
        priority_weights=weights,
        plans=plans,
        feasible_plan_ids=frozenset(feasible),
        utilities=utilities,
        ideal_utilities=ideal,
        weighted_welfare=welfare,
        pareto_plan_ids=frozenset(pareto),
        pareto_utility_vectors=pareto_vectors,
        representative_plan_id=representative,
        violations=violations,
    )


def _oracle_signature(view: OracleView) -> dict[str, Any]:
    return {
        "principal_ids": list(view.principal_ids),
        "priority_weights": view.priority_weights,
        "feasible_plan_ids": sorted(view.feasible_plan_ids),
        "utilities": {key: view.utilities[key] for key in sorted(view.feasible_plan_ids)},
        "ideal_utilities": view.ideal_utilities,
        "weighted_welfare": view.weighted_welfare,
        "pareto_plan_ids": sorted(view.pareto_plan_ids),
        "pareto_utility_vectors": [list(vector) for vector in view.pareto_utility_vectors],
        "representative_plan_id": view.representative_plan_id,
    }


def _discover_recompute() -> Callable[[Mapping[str, Any]], Mapping[str, Any]] | None:
    # Prefer the separately implemented exhaustive solver.  Falling back to
    # the primary solver keeps development fixtures usable, but formal
    # publication readiness normally records independent recomputation here.
    modules_and_names = (
        ("coordcap.validation", ("independent_solve_public_task",)),
        (
            "coordcap.oracle",
            ("compute_oracle", "solve_public_task", "build_oracle", "solve_task"),
        ),
        ("coordcap.generator", ("compute_oracle", "solve_public_task", "build_oracle")),
    )
    for module_name, names in modules_and_names:
        try:
            module = importlib.import_module(module_name)
        except (ImportError, ModuleNotFoundError):
            continue
        for name in names:
            function = getattr(module, name, None)
            if callable(function):
                return function
    return None


def _manifest_rows(manifest: Mapping[str, Any], label: str) -> list[dict[str, Any]]:
    rows = manifest.get("instances")
    if not isinstance(rows, list):
        raise EvaluationError(f"{label}.instances must be an array")
    if manifest.get("instance_count") != len(rows):
        raise EvaluationError(f"{label}.instance_count disagrees with instances")
    return [_as_dict(item, f"{label}.instances[{index}]") for index, item in enumerate(rows)]


def _validate_manifests(
    public_manifest: Mapping[str, Any],
    oracle_manifest: Mapping[str, Any],
    *,
    recompute_oracle: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, OracleView], bool]:
    required_top = {"protocol_version", "split", "master_seed", "instance_count", "instances"}
    for label, manifest in (("public", public_manifest), ("oracle", oracle_manifest)):
        missing = required_top - set(manifest)
        if missing:
            raise EvaluationError(f"{label} manifest is missing {sorted(missing)}")
        if manifest.get("protocol_version") != PROTOCOL_VERSION:
            raise EvaluationError(f"{label} manifest has wrong protocol_version")
        if manifest.get("split") not in {"smoke", "formal"}:
            raise EvaluationError(f"{label} manifest has an invalid split")
        if manifest.get("master_seed") != FROZEN_MASTER_SEED:
            raise EvaluationError(f"{label} manifest has the wrong master_seed")
    for field in ("protocol_version", "split", "master_seed", "instance_count"):
        if public_manifest.get(field) != oracle_manifest.get(field):
            raise EvaluationError(f"public/oracle manifest {field} values differ")
    public_rows = _manifest_rows(public_manifest, "public_manifest")
    oracle_rows = _manifest_rows(oracle_manifest, "oracle_manifest")
    if len(public_rows) != len(oracle_rows):
        raise EvaluationError("public/oracle instance counts differ")
    public_by_id: dict[str, dict[str, Any]] = {}
    views: dict[str, OracleView] = {}
    recompute_available = recompute_oracle is not None
    for index, (public, oracle_row) in enumerate(zip(public_rows, oracle_rows, strict=True)):
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
        missing_public = required_public - set(public)
        if missing_public:
            raise EvaluationError(
                f"public instance {index} is missing {sorted(missing_public)}"
            )
        required_oracle = {"instance_id", "public_sha256", "oracle", "oracle_sha256"}
        missing_oracle = required_oracle - set(oracle_row)
        if missing_oracle:
            raise EvaluationError(
                f"oracle instance {index} is missing {sorted(missing_oracle)}"
            )
        instance_id = _id_from(public, ("instance_id",), f"public instance {index}")
        if instance_id != oracle_row.get("instance_id"):
            raise EvaluationError("public/oracle instance order or IDs differ")
        if instance_id in public_by_id:
            raise EvaluationError(f"duplicate instance ID {instance_id}")
        task = _as_dict(public.get("public_task"), f"{instance_id}.public_task")
        declared_public_hash = public.get("public_sha256")
        computed_public_hash = canonical_sha256(task)
        if declared_public_hash != computed_public_hash:
            raise EvaluationError(f"{instance_id}: public_sha256 cannot be reproduced")
        if oracle_row.get("public_sha256") != declared_public_hash:
            raise EvaluationError(f"{instance_id}: oracle/public hash binding mismatch")
        oracle_obj = _as_dict(oracle_row.get("oracle"), f"{instance_id}.oracle")
        declared_oracle_hash = oracle_row.get("oracle_sha256")
        if declared_oracle_hash != canonical_sha256(oracle_obj):
            raise EvaluationError(f"{instance_id}: oracle_sha256 cannot be reproduced")
        stored = normalize_oracle(task, oracle_obj)
        if recompute_oracle is not None:
            recomputed_obj = recompute_oracle(task)
            recomputed = normalize_oracle(task, _as_dict(recomputed_obj, "recomputed oracle"))
            if _oracle_signature(stored) != _oracle_signature(recomputed):
                raise EvaluationError(f"{instance_id}: stored and recomputed oracle disagree")
        public_by_id[instance_id] = public
        views[instance_id] = stored
    return public_by_id, views, recompute_available


def _frozen_scope_audit(
    public_manifest: Mapping[str, Any],
    expected_runs: Sequence[Mapping[str, Any]],
    *,
    public_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Verify that an expected ledger is the frozen smoke/formal experiment.

    Terminal completeness alone is insufficient: a smaller ledger with every
    row present must never be relabeled as a complete formal matrix.
    """

    split = public_manifest.get("split")
    instances_raw = public_manifest.get("instances")
    instances = [dict(row) for row in instances_raw if isinstance(row, Mapping)] if isinstance(
        instances_raw, list
    ) else []
    public_by_id = {
        str(row["instance_id"]): row
        for row in instances
        if isinstance(row.get("instance_id"), str)
    }
    run_id_mismatches: list[str] = []
    if public_manifest_sha256 is not None:
        for run in expected_runs:
            identity = {
                "protocol_version": PROTOCOL_VERSION,
                "public_manifest_sha256": public_manifest_sha256,
                "instance_id": run.get("instance_id"),
                "order_variant": run.get("order_variant"),
                "model": run.get("model"),
                "method": run.get("method"),
                "max_answer_tokens": run.get("max_answer_tokens"),
                "max_model_calls": run.get("max_model_calls"),
            }
            derived = "coordcap_" + canonical_sha256(identity)[:24]
            if run.get("run_id") != derived:
                run_id_mismatches.append(str(run.get("run_id")))
    run_ids_match = not run_id_mismatches
    variants = Counter(str(run.get("order_variant")) for run in expected_runs)
    report: dict[str, Any] = {
        "split": split,
        "manifest_instance_count": len(instances),
        "expected_run_count": len(expected_runs),
        "order_variant_counts": dict(sorted(variants.items())),
        "run_id_derivation_checked": public_manifest_sha256 is not None,
        "run_id_mismatch_count": len(run_id_mismatches),
        "run_ids_match_frozen_derivation": run_ids_match,
        "complete": False,
    }

    if split == "formal":
        manifest_cells = Counter(
            (
                row.get("task_family"),
                row.get("principal_count"),
                row.get("conflict_level"),
            )
            for row in instances
        )
        expected_manifest_cells = {
            (family, principal_count, conflict)
            for family in FROZEN_TASK_FAMILIES
            for principal_count in FROZEN_PRINCIPAL_COUNTS
            for conflict in FROZEN_CONFLICT_LEVELS
        }
        manifest_grid_complete = (
            len(instances) == FORMAL_INSTANCE_COUNT
            and set(manifest_cells) == expected_manifest_cells
            and all(manifest_cells[key] == 4 for key in expected_manifest_cells)
        )

        canonical = [run for run in expected_runs if run.get("order_variant") == "canonical"]
        reverse = [run for run in expected_runs if run.get("order_variant") == "reverse"]
        no_other_variants = len(canonical) + len(reverse) == len(expected_runs)

        canonical_cells: Counter[tuple[Any, ...]] = Counter()
        canonical_instances: dict[tuple[Any, ...], set[str]] = defaultdict(set)
        metadata_bound = True
        canonical_pair_keys: set[tuple[Any, ...]] = set()
        for run in canonical:
            instance_id = run.get("instance_id")
            public = public_by_id.get(str(instance_id)) if isinstance(instance_id, str) else None
            if public is None:
                metadata_bound = False
                continue
            key = (
                run.get("model"),
                run.get("method"),
                run.get("max_answer_tokens"),
                run.get("max_model_calls"),
                public.get("principal_count"),
                public.get("conflict_level"),
                public.get("task_family"),
            )
            canonical_cells[key] += 1
            canonical_instances[key].add(instance_id)
            canonical_pair_keys.add(
                (
                    instance_id,
                    run.get("model"),
                    run.get("method"),
                    run.get("max_answer_tokens"),
                    run.get("max_model_calls"),
                )
            )
        expected_primary_cells = {
            (model, method, tokens, calls, principal_count, conflict, family)
            for model in FROZEN_MODELS
            for method in FROZEN_METHODS
            for tokens in FROZEN_TOKEN_BUDGETS
            for calls in FROZEN_CALL_BUDGETS
            for principal_count in FROZEN_PRINCIPAL_COUNTS
            for conflict in FROZEN_CONFLICT_LEVELS
            for family in FROZEN_TASK_FAMILIES
        }
        primary_grid_complete = (
            len(canonical) == FORMAL_CANONICAL_RUNS
            and set(canonical_cells) == expected_primary_cells
            and all(canonical_cells[key] == 4 for key in expected_primary_cells)
            and all(len(canonical_instances[key]) == 4 for key in expected_primary_cells)
        )

        panel_rows: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        reverse_settings_valid = True
        reverse_has_canonical_pair = True
        for run in reverse:
            instance_id = run.get("instance_id")
            if not isinstance(instance_id, str) or instance_id not in public_by_id:
                metadata_bound = False
                reverse_settings_valid = False
                continue
            panel_rows[instance_id].append(run)
            reverse_settings_valid = reverse_settings_valid and (
                run.get("model") in FROZEN_MODELS
                and run.get("method") in FROZEN_METHODS
                and run.get("max_answer_tokens") == 1024
                and run.get("max_model_calls") == 2
            )
            reverse_has_canonical_pair = reverse_has_canonical_pair and (
                (
                    instance_id,
                    run.get("model"),
                    run.get("method"),
                    1024,
                    2,
                )
                in canonical_pair_keys
            )
        expected_model_methods = {
            (model, method) for model in FROZEN_MODELS for method in FROZEN_METHODS
        }
        panel_per_instance_complete = bool(panel_rows) and all(
            len(rows) == len(expected_model_methods)
            and {(row.get("model"), row.get("method")) for row in rows}
            == expected_model_methods
            for rows in panel_rows.values()
        )
        panel_strata = Counter(
            (
                public_by_id[instance_id].get("task_family"),
                public_by_id[instance_id].get("principal_count"),
            )
            for instance_id in panel_rows
        )
        expected_panel_strata = {
            (family, principal_count)
            for family in FROZEN_TASK_FAMILIES
            for principal_count in FROZEN_PRINCIPAL_COUNTS
        }
        panel_conflicts = Counter(
            public_by_id[instance_id].get("conflict_level") for instance_id in panel_rows
        )
        frozen_panel_ids: set[str] = set()
        frozen_panel_selection_valid = True
        for family_index, family in enumerate(FROZEN_TASK_FAMILIES):
            for count_index, principal_count in enumerate(FROZEN_PRINCIPAL_COUNTS):
                conflict = FROZEN_CONFLICT_LEVELS[(family_index + count_index) % 3]
                matches = [
                    instance_id
                    for instance_id, row in public_by_id.items()
                    if row.get("task_family") == family
                    and row.get("principal_count") == principal_count
                    and row.get("conflict_level") == conflict
                    and instance_id.endswith("_r01")
                ]
                if len(matches) != 1:
                    frozen_panel_selection_valid = False
                else:
                    frozen_panel_ids.add(matches[0])
        panel_ids_match_frozen_selection = bool(
            frozen_panel_selection_valid
            and len(frozen_panel_ids) == FORMAL_CONSISTENCY_INSTANCES
            and set(panel_rows) == frozen_panel_ids
        )
        panel_complete = (
            len(reverse) == FORMAL_REVERSE_RUNS
            and len(panel_rows) == FORMAL_CONSISTENCY_INSTANCES
            and panel_per_instance_complete
            and set(panel_strata) == expected_panel_strata
            and all(panel_strata[key] == 1 for key in expected_panel_strata)
            and set(panel_conflicts) == set(FROZEN_CONFLICT_LEVELS)
            and all(panel_conflicts[level] == 8 for level in FROZEN_CONFLICT_LEVELS)
            and panel_ids_match_frozen_selection
            and reverse_settings_valid
            and reverse_has_canonical_pair
        )
        report.update(
            {
                "required_manifest_instances": FORMAL_INSTANCE_COUNT,
                "required_canonical_runs": FORMAL_CANONICAL_RUNS,
                "required_reverse_runs": FORMAL_REVERSE_RUNS,
                "required_total_runs": FORMAL_TOTAL_RUNS,
                "required_primary_cells": len(expected_primary_cells),
                "required_replicates_per_primary_cell": 4,
                "required_consistency_panel_instances": FORMAL_CONSISTENCY_INSTANCES,
                "manifest_grid_complete": manifest_grid_complete,
                "primary_grid_complete": primary_grid_complete,
                "consistency_panel_complete": panel_complete,
                "consistency_panel_ids_match_frozen_selection": panel_ids_match_frozen_selection,
                "metadata_bound": metadata_bound,
                "no_unfrozen_order_variants": no_other_variants,
            }
        )
        report["complete"] = bool(
            len(expected_runs) == FORMAL_TOTAL_RUNS
            and manifest_grid_complete
            and primary_grid_complete
            and panel_complete
            and metadata_bound
            and no_other_variants
            and run_ids_match
        )
        return report

    if split == "smoke":
        canonical = [run for run in expected_runs if run.get("order_variant") == "canonical"]
        per_instance: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        rows_valid = True
        for run in canonical:
            instance_id = run.get("instance_id")
            if not isinstance(instance_id, str) or instance_id not in public_by_id:
                rows_valid = False
                continue
            per_instance[instance_id].append(run)
            rows_valid = rows_valid and (
                run.get("model") in FROZEN_MODELS
                and run.get("method") in FROZEN_METHODS
                and run.get("max_answer_tokens") == 512
                and run.get("max_model_calls") == 2
            )
        expected_model_methods = {
            (model, method) for model in FROZEN_MODELS for method in FROZEN_METHODS
        }
        per_instance_complete = bool(per_instance) and all(
            len(rows) == len(expected_model_methods)
            and {(row.get("model"), row.get("method")) for row in rows}
            == expected_model_methods
            for rows in per_instance.values()
        )
        manifest_coverage = (
            {row.get("task_family") for row in instances} == set(FROZEN_TASK_FAMILIES)
            and {row.get("conflict_level") for row in instances} == set(FROZEN_CONFLICT_LEVELS)
            and {row.get("principal_count") for row in instances} == {2, 3, 4, 6, 8}
        )
        complete = (
            len(instances) == SMOKE_INSTANCE_COUNT
            and len(expected_runs) == SMOKE_TOTAL_RUNS
            and len(canonical) == len(expected_runs)
            and len(per_instance) == SMOKE_INSTANCE_COUNT
            and per_instance_complete
            and rows_valid
            and manifest_coverage
            and run_ids_match
        )
        report.update(
            {
                "required_manifest_instances": SMOKE_INSTANCE_COUNT,
                "required_total_runs": SMOKE_TOTAL_RUNS,
                "manifest_coverage_complete": manifest_coverage,
                "per_instance_model_method_grid_complete": per_instance_complete,
                "rows_use_frozen_smoke_budgets": rows_valid,
                "complete": complete,
            }
        )
    return report


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _terminal_contract_errors(record: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in (
        "protocol_version",
        "public_manifest_sha256",
        "public_sha256",
        "execution_config_sha256",
    ):
        if not isinstance(record.get(field), str) or not record.get(field):
            errors.append(field)
    status = record.get("status")
    if not isinstance(status, str) or not status:
        errors.append("status")
    for field in (
        "initial_json_valid",
        "effective_json_valid",
        "repair_used",
        "route_consistent",
    ):
        if not isinstance(record.get(field), bool):
            errors.append(field)
    if "parsed_output" not in record:
        errors.append("parsed_output")
    elif record.get("effective_json_valid") is True and not isinstance(
        record.get("parsed_output"), Mapping
    ):
        errors.append("parsed_output_for_valid_record")
    calls = record.get("calls")
    if not (
        isinstance(calls, list)
        or (isinstance(calls, int) and not isinstance(calls, bool) and calls >= 0)
    ):
        errors.append("calls")
    has_completion = "total_completion_tokens" in record or (
        isinstance(record.get("usage"), Mapping)
        and any(
            key in record["usage"]
            for key in ("completion_tokens", "output_tokens", "total_tokens")
        )
    )
    if not has_completion:
        errors.append("token_usage")
    completion_value = None
    if isinstance(record.get("usage"), Mapping):
        usage = record["usage"]
        completion_value = usage.get(
            "completion_tokens", usage.get("output_tokens", usage.get("total_tokens"))
        )
    if "total_completion_tokens" in record:
        completion_value = record.get("total_completion_tokens")
    if completion_value is not None and (
        isinstance(completion_value, bool)
        or not isinstance(completion_value, int)
        or completion_value < 0
    ):
        errors.append("token_usage_value")
    usage_complete = record.get("usage_complete")
    if usage_complete is not None and not isinstance(usage_complete, bool):
        errors.append("usage_complete")
    if usage_complete is True and completion_value is None:
        errors.append("usage_complete_without_tokens")
    budget_compliant = record.get("budget_compliant")
    if budget_compliant is not None and not isinstance(budget_compliant, bool):
        errors.append("budget_compliant")
    if usage_complete is False and budget_compliant is True:
        errors.append("budget_compliant_without_usage")
    if not any(
        key in record
        for key in ("reported_cost", "reported_cost_usd", "cost_usd", "cost")
    ):
        errors.append("reported_cost")
    if not any(
        key in record
        for key in ("end_to_end_latency_seconds", "latency_seconds", "latency")
    ):
        errors.append("latency")
    raw_paths = record.get("raw_paths")
    if not isinstance(raw_paths, list) or any(not isinstance(value, str) for value in raw_paths):
        errors.append("raw_paths")
    elif isinstance(calls, list):
        if len(calls) != len(raw_paths):
            errors.append("calls_raw_paths_length")
        for index, call in enumerate(calls):
            if not isinstance(call, Mapping):
                errors.append("call_record")
                continue
            if index < len(raw_paths) and call.get("raw_path") not in (None, raw_paths[index]):
                errors.append("call_raw_path_binding")
    latency_value = record.get(
        "end_to_end_latency_seconds", record.get("latency_seconds", record.get("latency"))
    )
    if isinstance(latency_value, Mapping):
        latency_value = latency_value.get("end_to_end_seconds", latency_value.get("seconds"))
    if latency_value is not None and _numeric(latency_value) is None:
        errors.append("latency_value")
    return errors


def _record_resource(record: Mapping[str, Any]) -> dict[str, float | int | None]:
    calls = record.get("calls")
    call_count: int | None
    if isinstance(calls, list):
        call_count = len(calls)
    elif isinstance(calls, int) and not isinstance(calls, bool) and calls >= 0:
        call_count = calls
    else:
        call_count = None

    usage = record.get("usage") if isinstance(record.get("usage"), Mapping) else {}
    completion = _numeric(
        usage.get(
            "completion_tokens",
            usage.get(
                "output_tokens",
                record.get("total_completion_tokens", record.get("completion_tokens")),
            ),
        )
    )
    total_tokens = _numeric(usage.get("total_tokens", record.get("total_tokens")))
    if total_tokens is None:
        prompt_total = _numeric(record.get("total_prompt_tokens"))
        completion_total = _numeric(record.get("total_completion_tokens"))
        if prompt_total is not None and completion_total is not None:
            total_tokens = prompt_total + completion_total
    if isinstance(calls, list):
        call_usages = [item.get("usage") for item in calls if isinstance(item, Mapping)]
        if completion is None and call_usages and all(isinstance(item, Mapping) for item in call_usages):
            values = [
                _numeric(item.get("completion_tokens", item.get("output_tokens")))
                for item in call_usages
            ]
            completion = sum(value for value in values if value is not None) if all(
                value is not None for value in values
            ) else None
        if total_tokens is None and call_usages and all(isinstance(item, Mapping) for item in call_usages):
            values = [_numeric(item.get("total_tokens")) for item in call_usages]
            total_tokens = sum(value for value in values if value is not None) if all(
                value is not None for value in values
            ) else None

    cost = _numeric(
        record.get(
            "reported_cost_usd",
            record.get(
                "reported_cost",
                record.get(
                    "cost_usd", record.get("cost", usage.get("cost_usd", usage.get("cost")))
                ),
            ),
        )
    )
    if record.get("reported_cost_complete") is False:
        cost = None
    latency_value = record.get(
        "end_to_end_latency_seconds", record.get("latency_seconds", record.get("latency"))
    )
    if isinstance(latency_value, Mapping):
        latency_value = latency_value.get("end_to_end_seconds", latency_value.get("seconds"))
    latency = _numeric(latency_value)
    return {
        "api_calls": call_count,
        "completion_tokens": completion,
        "total_tokens": total_tokens,
        "reported_cost_usd": cost,
        "end_to_end_latency_seconds": latency,
    }


def _aggregate_duplicate_resources(records: Sequence[Mapping[str, Any]]) -> dict[str, float | int | None]:
    per_record = [_record_resource(record) for record in records]
    output: dict[str, float | int | None] = {}
    for field in (
        "api_calls",
        "completion_tokens",
        "total_tokens",
        "reported_cost_usd",
        "end_to_end_latency_seconds",
    ):
        values = [item[field] for item in per_record]
        output[field] = (
            sum(value for value in values if value is not None)
            if values and all(value is not None for value in values)
            else None
        )
    return output


def _selected_plan_id(output: Mapping[str, Any]) -> str | None:
    selected = output.get("selected_plan")
    if isinstance(selected, str) and selected:
        return selected
    if isinstance(selected, Mapping):
        value = selected.get("plan_id", selected.get("id"))
        return value if isinstance(value, str) and value else None
    return None


def _hard_constraint_ids(public_task: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    output: dict[str, tuple[str, ...]] = {}
    principals = public_task.get("principals")
    if not isinstance(principals, list):
        return output
    for principal in principals:
        if not isinstance(principal, Mapping):
            continue
        principal_id = principal.get("principal_id", principal.get("id"))
        constraints = principal.get("hard_constraints")
        ids: list[str] = []
        if isinstance(constraints, list):
            for index, constraint in enumerate(constraints):
                if isinstance(constraint, Mapping):
                    value = constraint.get("constraint_id", constraint.get("id"))
                    ids.append(str(value) if value is not None else f"constraint_{index}")
                elif isinstance(constraint, str):
                    ids.append(constraint)
        if isinstance(principal_id, str):
            output[principal_id] = tuple(sorted(ids))
    return output


def _expected_resource_allocation(plan: Mapping[str, Any] | None) -> Any:
    if not isinstance(plan, Mapping):
        return {}
    if "resource_allocation" in plan:
        return plan.get("resource_allocation")
    decision = plan.get("decision")
    if isinstance(decision, Mapping):
        return decision.get("resource_allocation", {})
    return {}


def _report_consistency(
    output: Mapping[str, Any],
    *,
    plan_id: str | None,
    feasible: bool,
    view: OracleView,
    public_task: Mapping[str, Any],
) -> bool:
    outcomes = output.get("principal_outcomes")
    if not isinstance(outcomes, list):
        return False
    reported: dict[str, Mapping[str, Any]] = {}
    for item in outcomes:
        if not isinstance(item, Mapping):
            return False
        raw_principal_id = item.get("principal_id")
        if isinstance(raw_principal_id, bool) or not isinstance(raw_principal_id, (str, int)):
            return False
        principal_id = str(raw_principal_id)
        if principal_id in reported or item.get("utility") is not None:
            return False
        reported[principal_id] = item
    if set(reported) != set(view.principal_ids):
        return False
    expected_resource = _expected_resource_allocation(view.plans.get(plan_id or ""))
    if output.get("resource_allocation") != expected_resource:
        return False
    hard_ids = _hard_constraint_ids(public_task)
    violation_map = view.violations.get(plan_id or "", {})
    for principal_id in view.principal_ids:
        item = reported[principal_id]
        satisfied = item.get("satisfied_hard_constraints")
        violated = item.get("violated_hard_constraints")
        if not isinstance(satisfied, list) or not isinstance(violated, list):
            return False
        expected_violated = violation_map.get(principal_id, ())
        if feasible:
            expected_violated = ()
        expected_satisfied = tuple(
            sorted(set(hard_ids.get(principal_id, ())) - set(expected_violated))
        )
        if tuple(sorted(str(value) for value in violated)) != tuple(expected_violated):
            return False
        if tuple(sorted(str(value) for value in satisfied)) != expected_satisfied:
            return False
    return True


def _failure_category(row: Mapping[str, Any]) -> str:
    if row.get("record_state") == "missing":
        return "missing"
    if row.get("record_state") == "duplicate":
        return "duplicate"
    if not row.get("terminal_record"):
        return "nonterminal"
    if row.get("record_contract_errors"):
        return "invalid_terminal_contract"
    if row.get("protocol_invalid"):
        return "budget_or_identity_invalid"
    if not row.get("effective_json_valid"):
        return "invalid_json_or_transport"
    if row.get("abstain"):
        return "abstain"
    if row.get("unknown_plan"):
        return "unknown_plan"
    if not row.get("feasible"):
        return "hard_constraint_violation"
    if not row.get("pareto_efficient"):
        return "dominated_choice"
    if float(row.get("worst_principal_regret", 0.0)) > 0.25:
        return "high_regret"
    if row.get("resource_inconsistent"):
        return "resource_inconsistent"
    if not row.get("report_consistent"):
        return "report_inconsistency"
    if row.get("joint_success"):
        return "success"
    return "welfare_gap"


def _score_expected_run(
    expected: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    public: Mapping[str, Any],
    view: OracleView,
    *,
    public_manifest_sha256: str,
    execution_config_sha256: str,
) -> dict[str, Any]:
    record_state = "missing" if not records else "duplicate" if len(records) > 1 else "unique"
    record = records[0] if len(records) == 1 else {}
    status_value = record.get("status")
    status = (
        status_value
        if isinstance(status_value, str) and status_value
        else "missing"
        if not records
        else "duplicate"
        if len(records) > 1
        else "invalid_terminal"
    )
    contract_errors = _terminal_contract_errors(record) if len(records) == 1 else []
    terminal = (
        len(records) == 1
        and isinstance(status_value, str)
        and bool(status_value)
        and status.lower() not in NONTERMINAL_STATUSES
    )
    identity_mismatches = [
        field
        for field in RUN_IDENTITY_FIELDS
        if record and record.get(field) != expected.get(field)
    ]
    resources = (
        _record_resource(record)
        if len(records) == 1
        else _aggregate_duplicate_resources(records)
        if records
        else {
            "api_calls": None,
            "completion_tokens": None,
            "total_tokens": None,
            "reported_cost_usd": None,
            "end_to_end_latency_seconds": None,
        }
    )
    call_overrun = resources["api_calls"] is not None and int(resources["api_calls"]) > int(
        expected["max_model_calls"]
    )
    token_overrun = resources["completion_tokens"] is not None and float(
        resources["completion_tokens"]
    ) > int(expected["max_answer_tokens"])
    binding_mismatches: list[str] = []
    if record.get("protocol_version") != PROTOCOL_VERSION:
        binding_mismatches.append("protocol_version")
    if record.get("public_manifest_sha256") != public_manifest_sha256:
        binding_mismatches.append("public_manifest_sha256")
    if record.get("public_sha256") != public.get("public_sha256"):
        binding_mismatches.append("public_sha256")
    if record.get("execution_config_sha256") != execution_config_sha256:
        binding_mismatches.append("execution_config_sha256")
    budget_declared_invalid = record.get("budget_compliant") is False or status == "budget_violation"
    route_invalid = record.get("route_consistent") is False or status == "route_mismatch"
    protocol_invalid = bool(
        identity_mismatches
        or binding_mismatches
        or contract_errors
        or call_overrun
        or token_overrun
        or budget_declared_invalid
        or route_invalid
    )
    initial_valid = bool(len(records) == 1 and record.get("initial_json_valid") is True)
    effective_valid = bool(
        len(records) == 1
        and terminal
        and not protocol_invalid
        and record.get("effective_json_valid") is True
    )
    output = record.get("parsed_output") if isinstance(record.get("parsed_output"), Mapping) else {}
    abstain = bool(output.get("abstain") is True) if effective_valid else False
    plan_id = _selected_plan_id(output) if effective_valid and not abstain else None
    unknown_plan = bool(plan_id is not None and plan_id not in view.plans)
    expected_resource = _expected_resource_allocation(view.plans.get(plan_id or ""))
    resource_inconsistent = bool(
        effective_valid
        and not abstain
        and not unknown_plan
        and output.get("resource_allocation") != expected_resource
    )
    # Model-authored resource and outcome reports are diagnostic only.  The
    # selected plan ID is the sole semantic decision used by primary metrics.
    decision_valid = bool(
        effective_valid and not abstain and plan_id is not None and not unknown_plan
    )
    feasible = bool(decision_valid and plan_id in view.feasible_plan_ids)
    if feasible and plan_id is not None:
        principal_utilities = dict(view.utilities[plan_id])
        regrets = {
            principal_id: (view.ideal_utilities[principal_id] - principal_utilities[principal_id])
            / UTILITY_SCALE
            for principal_id in view.principal_ids
        }
        mean_regret = sum(regrets.values()) / len(regrets)
        worst_regret = max(regrets.values())
        welfare = view.weighted_welfare[plan_id]
        normalized_gap = (view.maximum_weighted_welfare - welfare) / (
            UTILITY_SCALE * sum(view.priority_weights.values())
        )
    else:
        principal_utilities = {}
        regrets = {principal_id: 1.0 for principal_id in view.principal_ids}
        mean_regret = 1.0
        worst_regret = 1.0
        welfare = None
        normalized_gap = None
    joint_success = bool(feasible and normalized_gap is not None and normalized_gap <= SUCCESS_WELFARE_GAP_MAX)
    pareto = bool(feasible and plan_id in view.pareto_plan_ids)
    report_consistent = bool(
        decision_valid
        and _report_consistency(
            output,
            plan_id=plan_id,
            feasible=feasible,
            view=view,
            public_task=public["public_task"],
        )
    )
    row: dict[str, Any] = {
        **{field: expected.get(field) for field in RUN_IDENTITY_FIELDS},
        "execution_config_sha256": execution_config_sha256,
        "logical_id": public.get("logical_id"),
        "task_family": public.get("task_family"),
        "principal_count": public.get("principal_count"),
        "conflict_level": public.get("conflict_level"),
        "atomic_conflict_density_bp": public.get("atomic_conflict_density_bp"),
        "record_state": record_state,
        "source_paths": sorted(
            str(item.get("_source_path")) for item in records if item.get("_source_path")
        ),
        "status": status,
        "terminal_record": terminal,
        "identity_mismatches": identity_mismatches,
        "binding_mismatches": binding_mismatches,
        "record_contract_errors": contract_errors,
        "protocol_invalid": protocol_invalid,
        "route_consistent": (
            record.get("route_consistent") is True if len(records) == 1 else False
        ),
        "call_budget_overrun": call_overrun,
        "completion_token_budget_overrun": token_overrun,
        "initial_json_valid": initial_valid,
        "effective_json_valid": effective_valid,
        "repair_used": bool(record.get("repair_used")) if len(records) == 1 else False,
        "abstain": abstain,
        "selected_plan_id": plan_id,
        "unknown_plan": unknown_plan,
        "resource_inconsistent": resource_inconsistent,
        "feasible": feasible,
        "hard_constraint_violation": int(not feasible),
        "principal_utilities": principal_utilities,
        "principal_regrets": regrets,
        "mean_principal_regret": mean_regret,
        "worst_principal_regret": worst_regret,
        "weighted_welfare": welfare,
        "normalized_weighted_welfare_gap": normalized_gap,
        "joint_success": int(joint_success),
        "pareto_efficient": int(pareto),
        "report_consistent": int(report_consistent),
        **resources,
    }
    row["failure_category"] = _failure_category(row)
    return row


def coordination_consistency(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    fields = ("logical_id", "model", "method", "max_answer_tokens", "max_model_calls")
    grouped: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[tuple(row.get(field) for field in fields)].append(row)
    pairs: list[dict[str, Any]] = []
    for key, selected in sorted(grouped.items(), key=lambda item: tuple(str(x) for x in item[0])):
        canonical = [row for row in selected if row.get("order_variant") == "canonical"]
        permuted = [row for row in selected if row.get("order_variant") != "canonical"]
        if not canonical or not permuted:
            continue
        for other in permuted:
            base = canonical[0]
            consistent = bool(
                len(canonical) == 1
                and base.get("effective_json_valid")
                and other.get("effective_json_valid")
                and base.get("feasible")
                and other.get("feasible")
                and base.get("principal_utilities") == other.get("principal_utilities")
            )
            pairs.append(
                {
                    **dict(zip(fields, key, strict=True)),
                    "canonical_run_id": base.get("run_id"),
                    "permuted_run_id": other.get("run_id"),
                    "permuted_order_variant": other.get("order_variant"),
                    "consistent": consistent,
                }
            )
    return {
        "pair_count": len(pairs),
        "consistent_pairs": sum(bool(pair["consistent"]) for pair in pairs),
        "rate": (
            sum(bool(pair["consistent"]) for pair in pairs) / len(pairs) if pairs else None
        ),
        "pairs": pairs,
    }


def evaluate_documents(
    public_manifest: Mapping[str, Any],
    oracle_manifest: Mapping[str, Any],
    expected_ledger: Mapping[str, Any],
    parsed_records: Sequence[Mapping[str, Any]],
    *,
    public_manifest_sha256: str | None = None,
    parsed_input_errors: Sequence[Mapping[str, Any]] = (),
    recompute_oracle: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    bootstrap_samples: int = BOOTSTRAP_SAMPLES,
    bootstrap_seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Evaluate documents while deriving one row per frozen expected run."""

    if expected_ledger.get("protocol_version") != PROTOCOL_VERSION:
        raise EvaluationError("expected ledger has wrong protocol_version")
    expected_top_fields = {
        "protocol_version",
        "public_manifest_sha256",
        "execution_config_sha256",
        "runs",
    }
    if set(expected_ledger) != expected_top_fields:
        raise EvaluationError(
            "expected ledger fields must be exactly protocol_version, "
            "public_manifest_sha256, execution_config_sha256, and runs"
        )
    if public_manifest_sha256 is None:
        public_manifest_sha256 = canonical_sha256(public_manifest)
    if expected_ledger.get("public_manifest_sha256") != public_manifest_sha256:
        raise EvaluationError("expected ledger is bound to a different public manifest")
    execution_config_sha256 = expected_ledger.get("execution_config_sha256")
    if not isinstance(execution_config_sha256, str) or not execution_config_sha256:
        raise EvaluationError("expected ledger execution_config_sha256 must be non-empty")
    if recompute_oracle is None:
        recompute_oracle = _discover_recompute()
    public_by_id, oracle_views, recompute_available = _validate_manifests(
        public_manifest, oracle_manifest, recompute_oracle=recompute_oracle
    )

    expected_runs = expected_ledger.get("runs")
    if not isinstance(expected_runs, list):
        raise EvaluationError("expected ledger runs must be an array")
    expected_by_id: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(expected_runs):
        run = _as_dict(value, f"expected runs[{index}]")
        missing = [field for field in RUN_IDENTITY_FIELDS if field not in run]
        if missing:
            raise EvaluationError(f"expected run {index} is missing {missing}")
        if set(run) != set(RUN_IDENTITY_FIELDS):
            raise EvaluationError(f"expected run {index} has non-frozen identity fields")
        for field in ("run_id", "instance_id", "order_variant", "model", "method"):
            if not isinstance(run.get(field), str) or not run.get(field):
                raise EvaluationError(f"expected run {index} has an invalid {field}")
        for field in ("max_answer_tokens", "max_model_calls"):
            if (
                isinstance(run.get(field), bool)
                or not isinstance(run.get(field), int)
                or int(run[field]) < 1
            ):
                raise EvaluationError(f"expected run {index} has an invalid {field}")
        run_id = run["run_id"]
        if run_id in expected_by_id:
            raise EvaluationError(f"duplicate expected run ID {run_id}")
        if run.get("instance_id") not in public_by_id:
            raise EvaluationError(f"expected run {run_id} references an unknown instance")
        expected_by_id[run_id] = run

    observed_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unattributed: list[dict[str, Any]] = []
    for value in parsed_records:
        record = dict(value)
        run_id = record.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            unattributed.append(record)
            continue
        observed_by_id[run_id].append(record)
    unknown_run_ids = sorted(set(observed_by_id) - set(expected_by_id))

    rows = [
        _score_expected_run(
            expected_by_id[run_id],
            observed_by_id.get(run_id, []),
            public_by_id[str(expected_by_id[run_id]["instance_id"])],
            oracle_views[str(expected_by_id[run_id]["instance_id"])],
            public_manifest_sha256=public_manifest_sha256,
            execution_config_sha256=execution_config_sha256,
        )
        for run_id in sorted(expected_by_id)
    ]
    consistency = coordination_consistency(rows)
    canonical_rows = [row for row in rows if row.get("order_variant") == "canonical"]
    all_cell_fields = (
        "model",
        "method",
        "max_answer_tokens",
        "max_model_calls",
        "principal_count",
        "conflict_level",
        "task_family",
    )
    aggregate_fields = all_cell_fields[:-1]
    cells_by_family = metric_cells(
        canonical_rows,
        group_fields=all_cell_fields,
        samples=bootstrap_samples,
        seed=bootstrap_seed,
    )
    aggregate_cells = metric_cells(
        canonical_rows,
        group_fields=aggregate_fields,
        samples=bootstrap_samples,
        seed=bootstrap_seed,
    )
    headline_cells = metric_cells(
        canonical_rows,
        group_fields=("model", "method"),
        samples=bootstrap_samples,
        seed=bootstrap_seed,
    )
    capacity = capacity_analysis(
        canonical_rows, samples=bootstrap_samples, seed=bootstrap_seed
    )

    status_counts = Counter(str(row["status"]) for row in rows)
    failure_counts = Counter(str(row["failure_category"]) for row in rows)
    missing_count = sum(row["record_state"] == "missing" for row in rows)
    duplicate_count = sum(row["record_state"] == "duplicate" for row in rows)
    nonterminal_count = sum(not row["terminal_record"] for row in rows)
    identity_mismatch_count = sum(bool(row["identity_mismatches"]) for row in rows)
    binding_mismatch_count = sum(bool(row["binding_mismatches"]) for row in rows)
    contract_invalid_count = sum(bool(row["record_contract_errors"]) for row in rows)
    protocol_invalid_count = sum(bool(row["protocol_invalid"]) for row in rows)
    frozen_scope = _frozen_scope_audit(
        public_manifest,
        list(expected_by_id.values()),
        public_manifest_sha256=public_manifest_sha256,
    )
    terminal_matrix_complete = bool(rows) and not any(
        (
            missing_count,
            duplicate_count,
            nonterminal_count,
            identity_mismatch_count,
            binding_mismatch_count,
            contract_invalid_count,
            len(unknown_run_ids),
            len(unattributed),
            len(parsed_input_errors),
        )
    )
    matrix_complete = bool(terminal_matrix_complete and frozen_scope["complete"])
    bootstrap_protocol_compliant = bool(
        bootstrap_samples == BOOTSTRAP_SAMPLES and bootstrap_seed == BOOTSTRAP_SEED
    )
    publication_ready = bool(
        matrix_complete
        and public_manifest.get("split") == "formal"
        and recompute_available
        and frozen_scope["complete"]
        and bootstrap_protocol_compliant
    )
    failure_examples = sorted(
        (row for row in rows if row["failure_category"] != "success"),
        key=lambda row: (
            str(row["failure_category"]),
            -float(row["worst_principal_regret"]),
            str(row["run_id"]),
        ),
    )
    return {
        "schema_version": "coordcap-evaluation-1.0.0",
        "protocol_version": PROTOCOL_VERSION,
        "execution_config_sha256": execution_config_sha256,
        "split": public_manifest.get("split"),
        "bootstrap": {"samples": bootstrap_samples, "seed": bootstrap_seed},
        "audit": {
            "expected_runs": len(expected_by_id),
            "observed_records": len(parsed_records),
            "scored_runs": len(rows),
            "missing_runs": missing_count,
            "duplicate_runs": duplicate_count,
            "nonterminal_runs": nonterminal_count,
            "identity_mismatch_runs": identity_mismatch_count,
            "binding_mismatch_runs": binding_mismatch_count,
            "record_contract_invalid_runs": contract_invalid_count,
            "protocol_invalid_runs": protocol_invalid_count,
            "unknown_observed_run_ids": unknown_run_ids,
            "unattributed_record_count": len(unattributed),
            "parsed_input_errors": list(parsed_input_errors),
            "oracle_recomputed_from_public": recompute_available,
            "terminal_matrix_complete": terminal_matrix_complete,
            "frozen_scope_complete": bool(frozen_scope["complete"]),
            "frozen_scope": frozen_scope,
            "bootstrap_protocol_compliant": bootstrap_protocol_compliant,
            "matrix_complete": matrix_complete,
            "publication_ready": publication_ready,
            "status_counts": dict(sorted(status_counts.items())),
            "failure_counts": dict(sorted(failure_counts.items())),
        },
        "scored_runs": rows,
        "metrics": {
            "headline_cells": headline_cells,
            "aggregate_cells": aggregate_cells,
            "cells_by_task_family": cells_by_family,
            "coordination_consistency": consistency,
        },
        "capacity": capacity,
        "failure_examples": failure_examples,
    }


def evaluate_files(
    public_manifest_path: str | Path,
    oracle_manifest_path: str | Path,
    expected_ledger_path: str | Path,
    parsed_root: str | Path | None,
    *,
    recompute_oracle: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    bootstrap_samples: int = BOOTSTRAP_SAMPLES,
    bootstrap_seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    public_path = Path(public_manifest_path)
    public = _as_dict(load_json(public_path), "public manifest")
    oracle = _as_dict(load_json(oracle_manifest_path), "oracle manifest")
    expected = _as_dict(load_json(expected_ledger_path), "expected ledger")
    records, errors = read_parsed_records(parsed_root)
    return evaluate_documents(
        public,
        oracle,
        expected,
        records,
        public_manifest_sha256=file_sha256(public_path),
        parsed_input_errors=errors,
        recompute_oracle=recompute_oracle,
        bootstrap_samples=bootstrap_samples,
        bootstrap_seed=bootstrap_seed,
    )


def write_evaluation(bundle: Mapping[str, Any], results_root: str | Path) -> dict[str, Path]:
    root = Path(results_root)
    root.mkdir(parents=True, exist_ok=True)
    evaluation_path = root / "evaluation_results.json"
    scored_path = root / "scored_runs.jsonl"
    bootstrap_path = root / "bootstrap_results.json"
    evaluation_path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with scored_path.open("w", encoding="utf-8") as handle:
        for row in bundle.get("scored_runs", []):
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    bootstrap_payload = {
        "schema_version": bundle.get("schema_version"),
        "protocol_version": bundle.get("protocol_version"),
        "execution_config_sha256": bundle.get("execution_config_sha256"),
        "split": bundle.get("split"),
        "bootstrap": bundle.get("bootstrap"),
        "metrics": bundle.get("metrics"),
        "capacity": bundle.get("capacity"),
        "audit": bundle.get("audit"),
    }
    bootstrap_path.write_text(
        json.dumps(bootstrap_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "evaluation": evaluation_path,
        "scored_runs": scored_path,
        "bootstrap": bootstrap_path,
    }
