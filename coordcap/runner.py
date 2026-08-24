"""Async, cached, resumable runner for the frozen CoordCap protocol.

Only ``public_task`` plus explicitly public instance metadata enters prompts.
This module is self-contained and does not import legacy project code.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .prompts import (
    ANALYSIS_SYSTEM,
    FINAL_SYSTEM,
    METHOD_INSTRUCTIONS,
    analysis_stage_names,
    build_analysis_messages,
    build_final_messages,
    build_repair_messages,
    canonical_json,
    ordered_public_task,
)
from .schema import (
    ANALYSIS_RESPONSE_SCHEMA,
    ANSWER_TOKEN_BUDGETS,
    CALL_BUDGETS,
    FINAL_RESPONSE_SCHEMA,
    METHODS,
    final_response_schema_for_task,
    parse_and_validate,
    response_format,
    strict_json_loads,
)


DEFAULT_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
RETRYABLE_HTTP_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
RUNNER_VERSION = "coordcap-runner-1.0"
FROZEN_TASK_FAMILIES = (
    "resource_allocation",
    "scheduling",
    "shared_plan_selection",
    "policy_choice",
    "constrained_recommendation",
    "conflicting_information_requests",
)
FROZEN_PRIMARY_COUNTS = (2, 4, 6, 8)
FROZEN_CONFLICT_LEVELS = ("low", "medium", "high")
FROZEN_MODELS = (
    "google/gemini-3.1-flash-lite",
    "openai/gpt-5.4-mini",
)
ROUTE_CONSISTENCY_RULE = "exact_returned_model_equals_requested_model"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_bytes(value: Any) -> bytes:
    # Shared CoordCap hashing contract: compact sorted JSON plus one LF.
    return (canonical_json(value) + "\n").encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def request_sha256(*, base_url: str, payload: Mapping[str, Any]) -> str:
    """Canonical request identity; authorization headers are never included."""

    return canonical_sha256({"base_url": base_url, "payload": dict(payload)})


def response_payload_sha256(payload: Mapping[str, Any]) -> str:
    return canonical_sha256(dict(payload))


def _load_strict_object(path: Path, *, label: str) -> dict[str, Any]:
    value, error = strict_json_loads(path.read_text(encoding="utf-8"))
    if error is not None or not isinstance(value, dict):
        raise RuntimeError(f"invalid {label} JSON at {path}: {error or 'not an object'}")
    return value


def _load_verified_raw(
    path: Path,
    *,
    base_url: str,
    expected_request_hash: str | None = None,
    expected_run_id: str | None = None,
) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"raw record is missing: {path}")
    record = _load_strict_object(path, label="raw record")
    request_payload = record.get("request_payload")
    response_payload = record.get("response_payload")
    if not isinstance(request_payload, Mapping) or not isinstance(response_payload, Mapping):
        raise RuntimeError(f"raw record lacks request/response payload objects: {path}")
    computed_request_hash = request_sha256(base_url=base_url, payload=request_payload)
    stored_request_hash = record.get("request_hash")
    if stored_request_hash != computed_request_hash:
        raise RuntimeError(f"raw request payload hash mismatch: {path}")
    if expected_request_hash is not None and computed_request_hash != expected_request_hash:
        raise RuntimeError(f"raw request differs from expected request: {path}")
    computed_response_hash = response_payload_sha256(response_payload)
    if record.get("response_payload_sha256") != computed_response_hash:
        raise RuntimeError(f"raw response payload hash mismatch: {path}")
    if expected_run_id is not None and record.get("run_id") != expected_run_id:
        raise RuntimeError(f"raw run_id mismatch: {path}")
    return record


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return slug or "unnamed"


def model_slug(model: str) -> str:
    return safe_slug(model.replace("/", "__"))


def _usage_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _as_optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _write_json_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    """Create one immutable JSON record; never replace an existing path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")


def _write_json_ledger(
    path: Path,
    value: Mapping[str, Any],
    *,
    overwrite: bool,
) -> None:
    """Write a reproducible expected-run ledger, refusing accidental drift."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if current == encoded:
            return
        if not overwrite:
            raise FileExistsError(f"expected ledger differs and overwrite is disabled: {path}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(path)


def load_public_manifest(path: Path) -> tuple[dict[str, Any], str]:
    """Load and validate the public-only manifest contract."""

    raw = path.read_bytes()
    value, error = strict_json_loads(raw.decode("utf-8"))
    if error is not None or not isinstance(value, dict):
        raise ValueError(f"invalid public manifest: {error or 'top level is not an object'}")
    required = {"protocol_version", "split", "master_seed", "instance_count", "instances"}
    missing = required - set(value)
    if missing:
        raise ValueError(f"public manifest missing fields: {sorted(missing)}")
    instances = value.get("instances")
    if not isinstance(instances, list):
        raise ValueError("public manifest instances must be an array")
    if value.get("instance_count") != len(instances):
        raise ValueError("public manifest instance_count does not match instances")
    required_instance = {
        "instance_id",
        "logical_id",
        "task_family",
        "principal_count",
        "conflict_level",
        "atomic_conflict_density_bp",
        "public_task",
        "public_sha256",
    }
    seen: set[str] = set()
    for index, instance in enumerate(instances):
        if not isinstance(instance, dict):
            raise ValueError(f"instances[{index}] is not an object")
        missing_instance = required_instance - set(instance)
        if missing_instance:
            raise ValueError(
                f"instances[{index}] missing fields: {sorted(missing_instance)}"
            )
        instance_id = str(instance["instance_id"])
        if instance_id in seen:
            raise ValueError(f"duplicate instance_id: {instance_id}")
        seen.add(instance_id)
        public_task = instance.get("public_task")
        if not isinstance(public_task, dict):
            raise ValueError(f"{instance_id}: public_task must be an object")
        actual_public_hash = canonical_sha256(public_task)
        if instance.get("public_sha256") != actual_public_hash:
            raise ValueError(f"{instance_id}: public_sha256 mismatch")
    return value, hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class RunSpec:
    protocol_version: str
    public_manifest_sha256: str
    run_id: str
    instance_id: str
    order_variant: str
    model: str
    method: str
    max_answer_tokens: int
    max_model_calls: int
    instance: Mapping[str, Any] = field(compare=False, repr=False)

    def ledger_record(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "instance_id": self.instance_id,
            "order_variant": self.order_variant,
            "model": self.model,
            "method": self.method,
            "max_answer_tokens": self.max_answer_tokens,
            "max_model_calls": self.max_model_calls,
        }


@dataclass
class RunnerConfig:
    public_manifest_path: Path
    models: Sequence[str]
    methods: Sequence[str] = METHODS
    max_answer_tokens: Sequence[int] = ANSWER_TOKEN_BUDGETS
    max_model_calls: Sequence[int] = CALL_BUDGETS
    order_variants: Sequence[str] = ("canonical",)
    enforce_frozen_smoke: bool = False
    include_frozen_reverse_panel: bool = False
    raw_root: Path = Path("coordcap/outputs/raw")
    parsed_root: Path = Path("coordcap/outputs/parsed")
    cache_root: Path = Path("coordcap/outputs/cache")
    audit_log: Path | None = None
    expected_ledger_path: Path | None = Path("coordcap/outputs/expected_runs.json")
    overwrite_expected_ledger: bool = False
    concurrency: int = 4
    timeout_seconds: float = 180.0
    temperature: float = 0.0
    api_seed: int = 0
    api_key: str | None = None
    base_url: str = DEFAULT_BASE_URL
    resume: bool = True
    use_cache: bool = True
    repair_invalid: bool = True

    def __post_init__(self) -> None:
        self.public_manifest_path = Path(self.public_manifest_path)
        self.raw_root = Path(self.raw_root)
        self.parsed_root = Path(self.parsed_root)
        self.cache_root = Path(self.cache_root)
        self.audit_log = Path(self.audit_log) if self.audit_log else self.raw_root / "api_calls.jsonl"
        self.expected_ledger_path = (
            Path(self.expected_ledger_path) if self.expected_ledger_path is not None else None
        )
        self.models = tuple(dict.fromkeys(str(item) for item in self.models if str(item)))
        self.methods = tuple(dict.fromkeys(str(item) for item in self.methods))
        self.max_answer_tokens = tuple(dict.fromkeys(int(item) for item in self.max_answer_tokens))
        self.max_model_calls = tuple(dict.fromkeys(int(item) for item in self.max_model_calls))
        self.order_variants = tuple(dict.fromkeys(str(item) for item in self.order_variants))
        if not self.models:
            raise ValueError("at least one model is required")
        unknown_methods = set(self.methods) - set(METHODS)
        if unknown_methods:
            raise ValueError(f"unknown methods: {sorted(unknown_methods)}")
        unknown_tokens = set(self.max_answer_tokens) - set(ANSWER_TOKEN_BUDGETS)
        if unknown_tokens:
            raise ValueError(f"non-frozen answer-token budgets: {sorted(unknown_tokens)}")
        unknown_calls = set(self.max_model_calls) - set(CALL_BUDGETS)
        if unknown_calls:
            raise ValueError(f"non-frozen call budgets: {sorted(unknown_calls)}")
        if not self.order_variants:
            raise ValueError("at least one order variant is required")
        if self.concurrency < 1 or self.timeout_seconds <= 0:
            raise ValueError("concurrency and timeout must be positive")


def _sampling_parameters(config: RunnerConfig, model: str) -> dict[str, Any]:
    parameters: dict[str, Any] = {"seed": config.api_seed}
    if model != "openai/gpt-5.4-mini":
        parameters["temperature"] = config.temperature
    return parameters


def execution_config_sha256(config: RunnerConfig) -> str:
    """Bind expected ledgers and terminals to the exact execution policy."""

    return canonical_sha256(
        {
            "runner_version": RUNNER_VERSION,
            "base_url": config.base_url,
            "models_in_order": list(config.models),
            "per_model_sampling": {
                model: _sampling_parameters(config, model) for model in config.models
            },
            "api_seed": config.api_seed,
            "repair_invalid": config.repair_invalid,
            "provider_require_parameters": True,
            "concurrency": config.concurrency,
            "timeout_seconds": config.timeout_seconds,
            "use_cache": config.use_cache,
            "resume": config.resume,
            "final_schema": FINAL_RESPONSE_SCHEMA,
            "analysis_schema": ANALYSIS_RESPONSE_SCHEMA,
            "prompt_contract": {
                "final_system": FINAL_SYSTEM,
                "analysis_system": ANALYSIS_SYSTEM,
                "method_instructions": METHOD_INSTRUCTIONS,
            },
        }
    )


def _run_id(
    *,
    protocol_version: str,
    public_manifest_sha256: str,
    instance_id: str,
    order_variant: str,
    model: str,
    method: str,
    max_answer_tokens: int,
    max_model_calls: int,
) -> str:
    identity = {
        "protocol_version": protocol_version,
        "public_manifest_sha256": public_manifest_sha256,
        "instance_id": instance_id,
        "order_variant": order_variant,
        "model": model,
        "method": method,
        "max_answer_tokens": max_answer_tokens,
        "max_model_calls": max_model_calls,
    }
    return "coordcap_" + canonical_sha256(identity)[:24]


def build_run_specs(
    config: RunnerConfig,
    manifest: Mapping[str, Any],
    public_manifest_sha256: str,
) -> list[RunSpec]:
    _validate_frozen_mode(config, manifest)
    protocol_version = str(manifest["protocol_version"])
    specs: list[RunSpec] = []
    for instance in manifest["instances"]:
        for order_variant in config.order_variants:
            # Validate that the requested variant is genuinely realizable.
            ordered_public_task(instance["public_task"], order_variant)
            for model in config.models:
                for method in config.methods:
                    for token_budget in config.max_answer_tokens:
                        for call_budget in config.max_model_calls:
                            identity = {
                                "protocol_version": protocol_version,
                                "public_manifest_sha256": public_manifest_sha256,
                                "instance_id": str(instance["instance_id"]),
                                "order_variant": order_variant,
                                "model": model,
                                "method": method,
                                "max_answer_tokens": token_budget,
                                "max_model_calls": call_budget,
                            }
                            specs.append(
                                RunSpec(
                                    **identity,
                                    run_id=_run_id(**identity),
                                    instance=instance,
                                )
                            )
    if config.enforce_frozen_smoke and len(specs) != 320:
        raise ValueError(f"frozen smoke ledger has {len(specs)} runs, expected 320")
    if config.include_frozen_reverse_panel:
        if len(specs) != 20_736:
            raise ValueError(f"frozen primary matrix has {len(specs)} runs, expected 20736")
        panel = _frozen_reverse_panel_instances(manifest["instances"])
        for instance in panel:
            ordered_public_task(instance["public_task"], "reverse")
            for model in config.models:
                for method in config.methods:
                    identity = {
                        "protocol_version": protocol_version,
                        "public_manifest_sha256": public_manifest_sha256,
                        "instance_id": str(instance["instance_id"]),
                        "order_variant": "reverse",
                        "model": model,
                        "method": method,
                        "max_answer_tokens": 1024,
                        "max_model_calls": 2,
                    }
                    specs.append(
                        RunSpec(
                            **identity,
                            run_id=_run_id(**identity),
                            instance=instance,
                        )
                    )
        if len(specs) != 20_928:
            raise ValueError(f"frozen formal ledger has {len(specs)} runs, expected 20928")
    run_ids = [spec.run_id for spec in specs]
    if len(run_ids) != len(set(run_ids)):
        raise ValueError("run_id collision in expected-run specification")
    return specs


def _validate_frozen_mode(
    config: RunnerConfig,
    manifest: Mapping[str, Any],
) -> None:
    """Fail closed before labeling an execution as a frozen panel."""

    if config.enforce_frozen_smoke and config.include_frozen_reverse_panel:
        raise ValueError("frozen smoke and frozen formal modes are mutually exclusive")
    if not config.enforce_frozen_smoke and not config.include_frozen_reverse_panel:
        return
    mode = "smoke" if config.enforce_frozen_smoke else "formal"
    expected_instances = 40 if mode == "smoke" else 288
    if manifest.get("split") != mode or len(manifest["instances"]) != expected_instances:
        raise ValueError(
            f"frozen {mode} mode requires the {expected_instances}-instance {mode} manifest"
        )
    if tuple(config.models) != FROZEN_MODELS:
        raise ValueError(f"frozen {mode} mode requires models {list(FROZEN_MODELS)}")
    if tuple(config.methods) != METHODS:
        raise ValueError(f"frozen {mode} mode requires all four methods in frozen order")
    expected_tokens = (512,) if mode == "smoke" else ANSWER_TOKEN_BUDGETS
    expected_calls = (2,) if mode == "smoke" else CALL_BUDGETS
    if tuple(config.max_answer_tokens) != expected_tokens:
        raise ValueError(
            f"frozen {mode} mode requires answer-token budgets {list(expected_tokens)}"
        )
    if tuple(config.max_model_calls) != expected_calls:
        raise ValueError(f"frozen {mode} mode requires call budgets {list(expected_calls)}")
    if tuple(config.order_variants) != ("canonical",):
        raise ValueError(f"frozen {mode} primary matrix requires canonical order")
    if config.temperature != 0.0:
        raise ValueError(f"frozen {mode} mode requires temperature=0")
    if config.api_seed != 0:
        raise ValueError(f"frozen {mode} mode requires api_seed=0")
    if not config.repair_invalid:
        raise ValueError(f"frozen {mode} mode requires repair_invalid=true")
    if config.base_url != DEFAULT_BASE_URL:
        raise ValueError(f"frozen {mode} mode requires base_url={DEFAULT_BASE_URL}")
    if not config.use_cache:
        raise ValueError(f"frozen {mode} mode requires content-addressed cache")
    if not config.resume:
        raise ValueError(f"frozen {mode} mode requires exact-identity resume")
    if mode == "smoke" and config.concurrency != 8:
        raise ValueError("frozen smoke mode requires concurrency=8")
    if mode == "smoke" and config.timeout_seconds != 180.0:
        raise ValueError("frozen smoke mode requires timeout_seconds=180")


def _frozen_reverse_panel_instances(
    instances: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Select the frozen 24-task order-consistency panel."""

    selected: list[Mapping[str, Any]] = []
    for family_index, family in enumerate(FROZEN_TASK_FAMILIES):
        for count_index, principal_count in enumerate(FROZEN_PRIMARY_COUNTS):
            conflict_level = FROZEN_CONFLICT_LEVELS[(family_index + count_index) % 3]
            matches = [
                instance
                for instance in instances
                if instance.get("task_family") == family
                and instance.get("principal_count") == principal_count
                and instance.get("conflict_level") == conflict_level
                and str(instance.get("instance_id", "")).endswith("_r01")
            ]
            if len(matches) != 1:
                raise ValueError(
                    "frozen reverse panel expected one r01 instance for "
                    f"{family}/p{principal_count}/{conflict_level}, found {len(matches)}"
                )
            selected.append(matches[0])
    if len(selected) != 24 or len({item["instance_id"] for item in selected}) != 24:
        raise ValueError("frozen reverse panel selection is not exactly 24 unique instances")
    return selected


def build_expected_run_ledger(config: RunnerConfig) -> dict[str, Any]:
    manifest, manifest_hash = load_public_manifest(config.public_manifest_path)
    specs = build_run_specs(config, manifest, manifest_hash)
    return {
        "protocol_version": str(manifest["protocol_version"]),
        "public_manifest_sha256": manifest_hash,
        "execution_config_sha256": execution_config_sha256(config),
        "runs": [spec.ledger_record() for spec in specs],
    }


def write_expected_run_ledger(config: RunnerConfig) -> dict[str, Any]:
    """Materialize the deterministic expected-run ledger without API access."""

    if config.expected_ledger_path is None:
        raise ValueError("expected_ledger_path is disabled")
    ledger = build_expected_run_ledger(config)
    _write_json_ledger(
        config.expected_ledger_path,
        ledger,
        overwrite=config.overwrite_expected_ledger,
    )
    return ledger


class OpenRouterTransport:
    """Small async OpenRouter-compatible transport with no secret logging."""

    def __init__(self, *, api_key: str, base_url: str) -> None:
        self.api_key = api_key
        self.base_url = base_url

    async def request(
        self,
        *,
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._request_sync, payload, timeout_seconds)

    def _request_sync(
        self,
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "coordcap/1.0",
            "X-Title": os.getenv("OPENROUTER_X_TITLE", "CoordCap"),
        }
        referer = os.getenv("OPENROUTER_HTTP_REFERER")
        if referer:
            headers["HTTP-Referer"] = referer
        request = urllib.request.Request(self.base_url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="replace")
                status_code = int(getattr(response, "status", 200))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:4000]
            return {
                "_coordcap_transport_success": False,
                "status_code": int(exc.code),
                "error": f"HTTP {exc.code}: {detail}",
                "retryable": int(exc.code) in RETRYABLE_HTTP_CODES,
            }
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            return {
                "_coordcap_transport_success": False,
                "status_code": None,
                "error": str(exc)[:4000],
                "retryable": True,
            }
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {
                "_coordcap_transport_success": False,
                "status_code": status_code,
                "error": f"non-JSON API response: {exc}; body={raw[:1000]}",
                "retryable": True,
            }
        if not isinstance(value, dict):
            return {
                "_coordcap_transport_success": False,
                "status_code": status_code,
                "error": "API response was not a JSON object",
                "retryable": False,
            }
        value["_coordcap_status_code"] = status_code
        return value


def _response_content(response: Mapping[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, Mapping):
        return ""
    message = first.get("message")
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(str(item["text"]))
        return "".join(parts)
    return str(content) if content is not None else ""


def _normalize_raw_record(
    *,
    response: Mapping[str, Any],
    request_hash: str,
    request_payload: Mapping[str, Any],
    source: str,
    latency_seconds: float,
    spec: RunSpec,
    call_index: int,
    stage: str,
    max_completion_tokens: int,
) -> dict[str, Any]:
    explicit_failure = response.get("_coordcap_transport_success") is False
    choices = response.get("choices")
    transport_success = not explicit_failure and isinstance(choices, list) and bool(choices)
    usage = response.get("usage") if isinstance(response.get("usage"), Mapping) else {}
    first_choice = choices[0] if transport_success and isinstance(choices[0], Mapping) else {}
    returned_model = str(response.get("model", ""))
    route_consistent = transport_success and returned_model == spec.model
    record = {
        "schema_version": "coordcap-raw-call-1.0",
        "timestamp_utc": utc_now(),
        "run_id": spec.run_id,
        "instance_id": spec.instance_id,
        "order_variant": spec.order_variant,
        "model": spec.model,
        "method": spec.method,
        "max_answer_tokens": spec.max_answer_tokens,
        "max_model_calls": spec.max_model_calls,
        "call_index": call_index,
        "stage": stage,
        "max_completion_tokens": max_completion_tokens,
        "request_hash": request_hash,
        "request_payload": dict(request_payload),
        "sampling_parameters": {
            key: request_payload[key]
            for key in ("temperature", "seed")
            if key in request_payload
        },
        "source": source,
        "transport_success": transport_success,
        "status_code": response.get("_coordcap_status_code", response.get("status_code")),
        "response_id": str(response.get("id", "")),
        "returned_model": returned_model,
        "returned_provider": str(response.get("provider", "")),
        "route_consistency_rule": ROUTE_CONSISTENCY_RULE,
        "route_consistent": route_consistent,
        "finish_reason": str(first_choice.get("finish_reason", "")),
        "raw_content": _response_content(response),
        "usage": dict(usage),
        "reported_cost": _as_optional_float(usage.get("cost")),
        "latency_seconds": latency_seconds,
        "error": None if transport_success else str(response.get("error", "missing choices")),
        "retryable": bool(response.get("retryable", False)),
        "response_payload": dict(response),
        "response_payload_sha256": response_payload_sha256(response),
    }
    return record


def _call_from_raw(record: Mapping[str, Any], *, raw_path: Path, source: str) -> dict[str, Any]:
    usage = record.get("usage") if isinstance(record.get("usage"), Mapping) else {}
    prompt_tokens = _usage_int(usage.get("prompt_tokens"))
    completion_tokens = _usage_int(usage.get("completion_tokens"))
    total_tokens = _usage_int(usage.get("total_tokens"))
    usage_complete = all(
        value is not None for value in (prompt_tokens, completion_tokens, total_tokens)
    )
    return {
        "call_index": int(record["call_index"]),
        "stage": str(record["stage"]),
        "request_hash": str(record["request_hash"]),
        "source": source,
        "raw_path": str(raw_path),
        "max_completion_tokens": int(record["max_completion_tokens"]),
        "transport_success": bool(record.get("transport_success")),
        "status_code": record.get("status_code"),
        "response_id": str(record.get("response_id", "")),
        "returned_model": str(record.get("returned_model", "")),
        "returned_provider": str(record.get("returned_provider", "")),
        "route_consistency_rule": str(
            record.get("route_consistency_rule", ROUTE_CONSISTENCY_RULE)
        ),
        "route_consistent": bool(record.get("route_consistent")),
        "sampling_parameters": dict(record.get("sampling_parameters", {})),
        "response_payload_sha256": str(record.get("response_payload_sha256", "")),
        "finish_reason": str(record.get("finish_reason", "")),
        "raw_content": str(record.get("raw_content", "")),
        "error": record.get("error"),
        "usage": dict(usage),
        "usage_complete": usage_complete,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "reported_cost": _as_optional_float(record.get("reported_cost")),
        "latency_seconds": _as_optional_float(record.get("latency_seconds")) or 0.0,
    }


def _primary_token_caps(total: int, call_count: int) -> list[int]:
    if call_count < 1:
        raise ValueError("invalid token-cap allocation")
    if call_count == 1:
        return [total]
    # Frozen allocation: final receives exactly 62.5%; intermediates share
    # the remaining 37.5% deterministically in stage order.
    final_cap = (total * 5) // 8
    analysis_total = total - final_cap
    if final_cap < 1:
        raise ValueError("answer-token budget is too small")
    base, remainder = divmod(analysis_total, call_count - 1)
    analysis_caps = [base + (1 if index < remainder else 0) for index in range(call_count - 1)]
    if any(cap < 1 for cap in analysis_caps):
        raise ValueError("analysis token cap is zero")
    return analysis_caps + [final_cap]


def _selected_plan_marker(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping) and "selected_plan" in value:
        return {"present": True, "value": value["selected_plan"]}
    return {"present": False}


class CoordCapRunner:
    def __init__(self, config: RunnerConfig, *, transport: Any | None = None) -> None:
        self.config = config
        self.transport = transport
        self._network_semaphore = asyncio.Semaphore(config.concurrency)
        self._audit_lock = asyncio.Lock()
        self._manifest: dict[str, Any] | None = None
        self._manifest_hash = ""
        self._execution_config_hash = execution_config_sha256(config)

    def _ensure_transport(self) -> None:
        if self.transport is not None:
            return
        api_key = self.config.api_key or os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        self.transport = OpenRouterTransport(api_key=api_key, base_url=self.config.base_url)

    def _parsed_path(self, spec: RunSpec) -> Path:
        return self.config.parsed_root / f"{spec.run_id}.json"

    def _raw_path(self, spec: RunSpec, call_index: int, stage: str) -> Path:
        return self.config.raw_root / spec.run_id / f"call_{call_index:02d}_{safe_slug(stage)}.json"

    async def _append_audit(self, event: Mapping[str, Any]) -> None:
        path = self.config.audit_log
        assert path is not None
        line = json.dumps(event, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        async with self._audit_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    async def _model_call(
        self,
        *,
        spec: RunSpec,
        call_index: int,
        stage: str,
        messages: Sequence[Mapping[str, Any]],
        schema: Mapping[str, Any],
        schema_name: str,
        max_completion_tokens: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": spec.model,
            "messages": [dict(message) for message in messages],
            "max_tokens": max_completion_tokens,
            "response_format": response_format(dict(schema), name=schema_name),
            "provider": {"require_parameters": True},
        }
        payload.update(_sampling_parameters(self.config, spec.model))
        request_hash = request_sha256(base_url=self.config.base_url, payload=payload)
        raw_path = self._raw_path(spec, call_index, stage)
        if raw_path.exists():
            record = _load_verified_raw(
                raw_path,
                base_url=self.config.base_url,
                expected_request_hash=request_hash,
                expected_run_id=spec.run_id,
            )
            call = _call_from_raw(record, raw_path=raw_path, source="raw_resume")
            await self._append_audit(
                {
                    "event": "model_call",
                    "timestamp_utc": utc_now(),
                    "run_id": spec.run_id,
                    **{key: value for key, value in call.items() if key != "raw_content"},
                }
            )
            return call

        cache_path = self.config.cache_root / f"{request_hash}.json"
        if self.config.use_cache and cache_path.exists():
            cached = _load_strict_object(cache_path, label="cache record")
            if cached.get("request_hash") != request_hash:
                raise RuntimeError(f"cache request hash mismatch: {cache_path}")
            response = cached.get("response_payload")
            if not isinstance(response, Mapping):
                raise RuntimeError(f"cache response payload is missing: {cache_path}")
            computed_response_hash = response_payload_sha256(response)
            if cached.get("response_payload_sha256") != computed_response_hash:
                raise RuntimeError(f"cache response payload hash mismatch: {cache_path}")
            latency_seconds = float(cached.get("latency_seconds", 0.0))
            source = "cache"
        else:
            self._ensure_transport()
            started = time.perf_counter()
            try:
                async with self._network_semaphore:
                    response = await self.transport.request(
                        payload=payload,
                        timeout_seconds=self.config.timeout_seconds,
                    )
            except Exception as exc:  # terminal transport record, never silent exclusion
                response = {
                    "_coordcap_transport_success": False,
                    "status_code": None,
                    "error": f"{type(exc).__name__}: {exc}"[:4000],
                    "retryable": True,
                }
            latency_seconds = time.perf_counter() - started
            source = "network"

        record = _normalize_raw_record(
            response=response,
            request_hash=request_hash,
            request_payload=payload,
            source=source,
            latency_seconds=latency_seconds,
            spec=spec,
            call_index=call_index,
            stage=stage,
            max_completion_tokens=max_completion_tokens,
        )
        _write_json_exclusive(raw_path, record)
        if self.config.use_cache and source == "network" and record["transport_success"]:
            cache_value = {
                "schema_version": "coordcap-cache-1.0",
                "request_hash": request_hash,
                "response_payload": dict(response),
                "response_payload_sha256": response_payload_sha256(response),
                "latency_seconds": latency_seconds,
            }
            try:
                _write_json_exclusive(cache_path, cache_value)
            except FileExistsError:
                pass
        call = _call_from_raw(record, raw_path=raw_path, source=source)
        await self._append_audit(
            {
                "event": "model_call",
                "timestamp_utc": utc_now(),
                "run_id": spec.run_id,
                **{key: value for key, value in call.items() if key != "raw_content"},
            }
        )
        return call

    def _public_metadata(self, spec: RunSpec) -> dict[str, Any]:
        instance = spec.instance
        return {
            "instance_id": spec.instance_id,
            "logical_id": instance["logical_id"],
            "task_family": instance["task_family"],
            "principal_count": instance["principal_count"],
            "conflict_level": instance["conflict_level"],
            "atomic_conflict_density_bp": instance["atomic_conflict_density_bp"],
            "public_sha256": instance["public_sha256"],
            "order_variant": spec.order_variant,
        }

    async def _execute(self, spec: RunSpec) -> dict[str, Any]:
        parsed_path = self._parsed_path(spec)
        if parsed_path.exists():
            if not self.config.resume:
                raise FileExistsError(f"parsed terminal already exists: {parsed_path}")
            record = _load_strict_object(parsed_path, label="parsed terminal")
            if record.get("run_id") != spec.run_id:
                raise RuntimeError(f"parsed run identity mismatch: {parsed_path}")
            if record.get("execution_config_sha256") != self._execution_config_hash:
                raise RuntimeError(f"parsed execution config mismatch: {parsed_path}")
            calls = record.get("calls")
            raw_paths = record.get("raw_paths")
            if not isinstance(calls, list) or not isinstance(raw_paths, list):
                raise RuntimeError(f"parsed terminal lacks calls/raw_paths: {parsed_path}")
            if len(calls) != len(raw_paths):
                raise RuntimeError(f"parsed calls/raw_paths length mismatch: {parsed_path}")
            for call, raw_path_text in zip(calls, raw_paths, strict=True):
                if not isinstance(call, Mapping) or not isinstance(raw_path_text, str):
                    raise RuntimeError(f"malformed parsed call reference: {parsed_path}")
                if call.get("raw_path") != raw_path_text:
                    raise RuntimeError(f"parsed raw path disagreement: {parsed_path}")
                raw_record = _load_verified_raw(
                    Path(raw_path_text),
                    base_url=self.config.base_url,
                    expected_request_hash=str(call.get("request_hash", "")),
                    expected_run_id=spec.run_id,
                )
                if call.get("response_payload_sha256") != raw_record.get(
                    "response_payload_sha256"
                ):
                    raise RuntimeError(f"parsed/raw response hash disagreement: {parsed_path}")
            result = dict(record)
            result["_resumed"] = True
            return result

        public_task = ordered_public_task(spec.instance["public_task"], spec.order_variant)
        final_schema = final_response_schema_for_task(public_task)
        metadata = self._public_metadata(spec)
        stages = analysis_stage_names(spec.method, spec.max_model_calls)
        if spec.method == "sequential_aggregation":
            principals = public_task.get("principals")
            principal_count = len(principals) if isinstance(principals, list) else 0
            stages = stages[: min(len(stages), principal_count)]
        primary_call_count = len(stages) + 1
        caps = _primary_token_caps(spec.max_answer_tokens, primary_call_count)
        calls: list[dict[str, Any]] = []
        notes: list[str] = []
        for stage_index, stage in enumerate(stages):
            messages = build_analysis_messages(
                method=spec.method,
                stage=stage,
                public_task=public_task,
                metadata=metadata,
                prior_notes=notes,
                stage_index=stage_index,
                stage_count=len(stages),
            )
            call = await self._model_call(
                spec=spec,
                call_index=len(calls),
                stage=stage,
                messages=messages,
                schema=ANALYSIS_RESPONSE_SCHEMA,
                schema_name="coordcap_analysis",
                max_completion_tokens=caps[stage_index],
            )
            candidate, valid, errors = parse_and_validate(call["raw_content"], final=False)
            call["json_valid"] = valid
            call["validation_errors"] = errors
            calls.append(call)
            if valid:
                notes.append(canonical_json(candidate))
            elif call["raw_content"]:
                notes.append(canonical_json({"invalid_stage": stage, "raw": call["raw_content"][:8000]}))
            else:
                notes.append(canonical_json({"failed_stage": stage, "error": call["error"]}))

        final_messages = build_final_messages(
            method=spec.method,
            public_task=public_task,
            metadata=metadata,
            prior_notes=notes,
            final_schema=final_schema,
        )
        initial_call = await self._model_call(
            spec=spec,
            call_index=len(calls),
            stage="final",
            messages=final_messages,
            schema=final_schema,
            schema_name="coordcap_final",
            max_completion_tokens=caps[-1],
        )
        initial_candidate, initial_valid, initial_errors = parse_and_validate(
            initial_call["raw_content"],
            final=True,
            schema=final_schema,
        )
        initial_call["json_valid"] = initial_valid
        initial_call["validation_errors"] = initial_errors
        calls.append(initial_call)

        repair_used = False
        repair_decision_preservation_verifiable = False
        repair_decision_changed = False
        effective_candidate = initial_candidate
        effective_valid = initial_valid
        effective_errors = initial_errors
        effective_call = initial_call
        usage_complete_before_repair = all(bool(call["usage_complete"]) for call in calls)
        remaining_actual_tokens = (
            spec.max_answer_tokens
            - sum(int(call["completion_tokens"]) for call in calls)
            if usage_complete_before_repair
            else 0
        )
        if (
            not initial_valid
            and self.config.repair_invalid
            and len(calls) < spec.max_model_calls
            and usage_complete_before_repair
            and remaining_actual_tokens > 0
        ):
            repair_used = True
            repair_call = await self._model_call(
                spec=spec,
                call_index=len(calls),
                stage="repair_final",
                messages=build_repair_messages(
                    invalid_text=initial_call["raw_content"],
                    validation_errors=initial_errors,
                    final_schema=final_schema,
                ),
                schema=final_schema,
                schema_name="coordcap_final_repair",
                max_completion_tokens=remaining_actual_tokens,
            )
            repair_candidate, repair_valid, repair_errors = parse_and_validate(
                repair_call["raw_content"],
                final=True,
                schema=final_schema,
            )
            repair_call["json_valid"] = repair_valid
            repair_call["validation_errors"] = repair_errors
            initial_parsed_value, initial_parse_error = strict_json_loads(
                initial_call["raw_content"]
            )
            repair_parsed_value, repair_parse_error = strict_json_loads(
                repair_call["raw_content"]
            )
            repair_decision_preservation_verifiable = (
                initial_parse_error is None and repair_parse_error is None
            )
            if repair_decision_preservation_verifiable:
                repair_decision_changed = _selected_plan_marker(
                    initial_parsed_value
                ) != _selected_plan_marker(repair_parsed_value)
            repair_call["repair_decision_preservation_verifiable"] = (
                repair_decision_preservation_verifiable
            )
            repair_call["repair_decision_changed"] = repair_decision_changed
            calls.append(repair_call)
            effective_call = repair_call
            effective_valid = repair_valid and not repair_decision_changed
            effective_errors = list(repair_errors)
            if repair_decision_changed:
                effective_errors.append("repair_changed_selected_plan")
            # Preserve a parseable invalid initial candidate if repair transport
            # or syntax fails; invalid records remain inspectable.
            effective_candidate = (
                repair_candidate if repair_candidate is not None else initial_candidate
            )

        usage_complete = bool(calls) and all(bool(call["usage_complete"]) for call in calls)
        route_consistent = bool(calls) and all(
            bool(call["route_consistent"]) for call in calls
        )
        route_mismatch = any(
            bool(call["transport_success"]) and not bool(call["route_consistent"])
            for call in calls
        )
        total_prompt_tokens = (
            sum(int(call["prompt_tokens"]) for call in calls) if usage_complete else None
        )
        total_completion_tokens = (
            sum(int(call["completion_tokens"]) for call in calls) if usage_complete else None
        )
        total_requested_cap = sum(int(call["max_completion_tokens"]) for call in calls)
        costs = [call["reported_cost"] for call in calls]
        reported_cost_partial = sum(float(cost) for cost in costs if cost is not None)
        reported_cost_complete = bool(costs) and all(cost is not None for cost in costs)
        reported_cost = reported_cost_partial if reported_cost_complete else None
        latency_seconds = sum(float(call["latency_seconds"]) for call in calls)
        budget_compliant = (
            usage_complete
            and total_completion_tokens is not None
            and len(calls) <= spec.max_model_calls
            and total_completion_tokens <= spec.max_answer_tokens
        )
        if not usage_complete:
            status = "usage_unavailable"
        elif not budget_compliant:
            status = "budget_violation"
        elif route_mismatch:
            status = "route_mismatch"
        elif effective_valid:
            status = "complete"
        elif not effective_call["transport_success"] and not initial_call["transport_success"]:
            status = "transport_error"
        else:
            status = "invalid_output"

        terminal = {
            "schema_version": "coordcap-parsed-terminal-1.0",
            "runner_version": RUNNER_VERSION,
            "execution_config_sha256": self._execution_config_hash,
            "protocol_version": spec.protocol_version,
            "public_manifest_sha256": spec.public_manifest_sha256,
            **spec.ledger_record(),
            "logical_id": spec.instance["logical_id"],
            "task_family": spec.instance["task_family"],
            "principal_count": spec.instance["principal_count"],
            "conflict_level": spec.instance["conflict_level"],
            "atomic_conflict_density_bp": spec.instance["atomic_conflict_density_bp"],
            "public_sha256": spec.instance["public_sha256"],
            "status": status,
            "initial_json_valid": initial_valid,
            "effective_json_valid": effective_valid,
            "repair_used": repair_used,
            "repair_decision_preservation_verifiable": (
                repair_decision_preservation_verifiable
            ),
            "repair_decision_changed": repair_decision_changed,
            "parsed_output": effective_candidate,
            "initial_parsed_output": initial_candidate,
            "initial_validation_errors": initial_errors,
            "effective_validation_errors": effective_errors,
            "calls": calls,
            "call_count": len(calls),
            "usage_complete": usage_complete,
            "usage_note": (
                None if usage_complete else "one_or_more_calls_missing_integer_token_usage"
            ),
            "total_prompt_tokens": total_prompt_tokens,
            "total_completion_tokens": total_completion_tokens,
            "requested_completion_token_cap": spec.max_answer_tokens,
            "sum_call_max_tokens": total_requested_cap,
            "reported_cost": reported_cost,
            "reported_cost_partial": reported_cost_partial,
            "reported_cost_complete": reported_cost_complete,
            "latency_seconds": latency_seconds,
            "budget_compliant": budget_compliant,
            "route_consistency_rule": ROUTE_CONSISTENCY_RULE,
            "route_consistent": route_consistent,
            "raw_paths": [call["raw_path"] for call in calls],
            "created_at_utc": utc_now(),
        }
        _write_json_exclusive(parsed_path, terminal)
        await self._append_audit(
            {
                "event": "terminal_record",
                "timestamp_utc": utc_now(),
                "run_id": spec.run_id,
                "status": status,
                "initial_json_valid": initial_valid,
                "effective_json_valid": effective_valid,
                "repair_used": repair_used,
                "repair_decision_preservation_verifiable": (
                    repair_decision_preservation_verifiable
                ),
                "repair_decision_changed": repair_decision_changed,
                "call_count": len(calls),
                "usage_complete": usage_complete,
                "route_consistency_rule": ROUTE_CONSISTENCY_RULE,
                "route_consistent": route_consistent,
                "total_prompt_tokens": total_prompt_tokens,
                "total_completion_tokens": total_completion_tokens,
                "reported_cost": reported_cost,
                "reported_cost_complete": reported_cost_complete,
                "latency_seconds": latency_seconds,
                "parsed_path": str(parsed_path),
            }
        )
        return terminal

    async def _execute_safe(self, spec: RunSpec) -> dict[str, Any]:
        try:
            return await self._execute(spec)
        except Exception as exc:
            parsed_path = self._parsed_path(spec)
            if parsed_path.exists():
                raise
            terminal = {
                "schema_version": "coordcap-parsed-terminal-1.0",
                "runner_version": RUNNER_VERSION,
                "execution_config_sha256": self._execution_config_hash,
                "protocol_version": spec.protocol_version,
                "public_manifest_sha256": spec.public_manifest_sha256,
                **spec.ledger_record(),
                "logical_id": spec.instance["logical_id"],
                "task_family": spec.instance["task_family"],
                "principal_count": spec.instance["principal_count"],
                "conflict_level": spec.instance["conflict_level"],
                "atomic_conflict_density_bp": spec.instance["atomic_conflict_density_bp"],
                "public_sha256": spec.instance["public_sha256"],
                "status": "runner_error",
                "initial_json_valid": False,
                "effective_json_valid": False,
                "repair_used": False,
                "repair_decision_preservation_verifiable": False,
                "repair_decision_changed": False,
                "parsed_output": None,
                "initial_parsed_output": None,
                "initial_validation_errors": [],
                "effective_validation_errors": [],
                "calls": [],
                "call_count": 0,
                "usage_complete": False,
                "usage_note": "runner_error_before_complete_usage_accounting",
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "requested_completion_token_cap": spec.max_answer_tokens,
                "sum_call_max_tokens": 0,
                "reported_cost": None,
                "reported_cost_partial": 0.0,
                "reported_cost_complete": False,
                "latency_seconds": 0.0,
                "budget_compliant": False,
                "route_consistency_rule": ROUTE_CONSISTENCY_RULE,
                "route_consistent": False,
                "raw_paths": [],
                "error": f"{type(exc).__name__}: {exc}"[:4000],
                "created_at_utc": utc_now(),
            }
            _write_json_exclusive(parsed_path, terminal)
            await self._append_audit(
                {
                    "event": "terminal_record",
                    "timestamp_utc": utc_now(),
                    "run_id": spec.run_id,
                    "status": "runner_error",
                    "error": terminal["error"],
                    "parsed_path": str(parsed_path),
                }
            )
            return terminal

    async def run(self) -> dict[str, Any]:
        manifest, manifest_hash = load_public_manifest(self.config.public_manifest_path)
        self._manifest = manifest
        self._manifest_hash = manifest_hash
        specs = build_run_specs(self.config, manifest, manifest_hash)
        ledger = {
            "protocol_version": str(manifest["protocol_version"]),
            "public_manifest_sha256": manifest_hash,
            "execution_config_sha256": self._execution_config_hash,
            "runs": [spec.ledger_record() for spec in specs],
        }
        if self.config.expected_ledger_path is not None:
            _write_json_ledger(
                self.config.expected_ledger_path,
                ledger,
                overwrite=self.config.overwrite_expected_ledger,
            )
        if any(not self._parsed_path(spec).exists() for spec in specs):
            self._ensure_transport()
        results = await asyncio.gather(*(self._execute_safe(spec) for spec in specs))
        resumed = sum(bool(result.pop("_resumed", False)) for result in results)
        statuses = Counter(str(result.get("status", "unknown")) for result in results)
        return {
            "protocol_version": str(manifest["protocol_version"]),
            "public_manifest_sha256": manifest_hash,
            "execution_config_sha256": self._execution_config_hash,
            "expected_runs": len(specs),
            "terminal_records": len(results),
            "resumed_records": resumed,
            "status_counts": dict(sorted(statuses.items())),
            "expected_ledger_path": (
                str(self.config.expected_ledger_path)
                if self.config.expected_ledger_path is not None
                else None
            ),
        }


async def run_coordcap(config: RunnerConfig, *, transport: Any | None = None) -> dict[str, Any]:
    return await CoordCapRunner(config, transport=transport).run()
