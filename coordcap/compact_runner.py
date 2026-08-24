"""Isolated runner for the compact CoordCap 1.1 corrected smoke."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import canonical_json, canonical_sha256
from .compact_protocol import (
    COMPACT_METHOD_INSTRUCTIONS,
    COMPACT_PROTOCOL_VERSION,
    COMPACT_SCHEMA_VERSION,
    COMPACT_SYSTEM,
    CORRECTED_SMOKE_MAX_OUTPUT_TOKENS,
    CORRECTED_SMOKE_MAX_SEMANTIC_CALLS,
    CORRECTED_SMOKE_MODELS,
    CORRECTED_SMOKE_PRIMARY_CAP,
    CORRECTED_SMOKE_REPAIR_CAP,
    CORRECTED_SMOKE_TRANSPORT_RETRIES,
    FORMAL_MAIN_PROTOCOL_VERSION,
    build_compact_messages,
    build_compact_repair_messages,
    compact_schema_for_task,
    decode_compact_decision,
    extract_compact_marker,
    parse_compact_response,
)
from .runner import (
    DEFAULT_BASE_URL,
    OpenRouterTransport,
    response_payload_sha256,
    utc_now,
)
from .schema import METHODS, response_format, strict_json_loads


COMPACT_RUNNER_VERSION = "coordcap-compact-runner-1.1.1"
FORMAL_MAIN_RUNNER_VERSION = "coordcap-formal-main-runner-1.0.0"
ROUTE_CONSISTENCY_RULE = "exact_returned_model_equals_requested_model"
FORBIDDEN_REQUEST_TERMS = (
    "ideal_utilities",
    "feasible_plan_ids",
    "pareto_plan_ids",
    "gold_plan_id",
    "gold_weighted_welfare",
    "regret_bp_by_plan",
    "weighted_welfare_by_plan",
    "oracle_schema_version",
    "evaluator feedback",
    "api key",
)


def _canonical_bytes(value: Any) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_exclusive(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(canonical_json(value) + "\n")


def _write_ledger(path: Path, value: Any, *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _canonical_bytes(value)
    if path.exists():
        if path.read_bytes() == data:
            return
        if not overwrite:
            raise FileExistsError(f"ledger differs and overwrite is disabled: {path}")
    path.write_bytes(data)


def _load_object(path: Path, label: str) -> dict[str, Any]:
    value, error = strict_json_loads(path.read_text(encoding="utf-8"))
    if error is not None or not isinstance(value, dict):
        raise RuntimeError(f"invalid {label} at {path}: {error or 'not an object'}")
    return value


def _response_content(response: Mapping[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        return ""
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item["text"])
            for item in content
            if isinstance(item, Mapping) and isinstance(item.get("text"), str)
        )
    return str(content) if content is not None else ""


def _usage_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _request_hash(base_url: str, payload: Mapping[str, Any]) -> str:
    return canonical_sha256({"base_url": base_url, "payload": dict(payload)})


def _deterministic_jitter(run_id: str, call_index: int, retry_index: int) -> float:
    digest = hashlib.sha256(f"{run_id}|{call_index}|{retry_index}".encode()).digest()
    return int.from_bytes(digest[:2], "big") / 65535.0 * 0.25


@dataclass(frozen=True)
class CompactRunSpec:
    protocol_version: str
    public_manifest_sha256: str
    run_id: str
    instance_id: str
    model: str
    method: str
    max_output_tokens: int
    max_semantic_calls: int
    instance: Mapping[str, Any] = field(compare=False, repr=False)

    def ledger_record(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "instance_id": self.instance_id,
            "model": self.model,
            "method": self.method,
            "max_output_tokens": self.max_output_tokens,
            "max_semantic_calls": self.max_semantic_calls,
        }


@dataclass
class CompactRunnerConfig:
    public_manifest_path: Path
    models: Sequence[str] = CORRECTED_SMOKE_MODELS
    methods: Sequence[str] = METHODS
    raw_root: Path = Path("coordcap/outputs/corrected_smoke/raw")
    parsed_root: Path = Path("coordcap/outputs/corrected_smoke/parsed")
    cache_root: Path = Path("coordcap/outputs/corrected_smoke/cache")
    audit_log: Path = Path("coordcap/audits/corrected_smoke_call_audit.jsonl")
    expected_ledger_path: Path = Path(
        "coordcap/data/manifests/corrected_smoke.expected_runs.json"
    )
    overwrite_expected_ledger: bool = False
    max_output_tokens: int = CORRECTED_SMOKE_MAX_OUTPUT_TOKENS
    primary_cap: int = CORRECTED_SMOKE_PRIMARY_CAP
    repair_cap: int = CORRECTED_SMOKE_REPAIR_CAP
    max_semantic_calls: int = CORRECTED_SMOKE_MAX_SEMANTIC_CALLS
    transport_retries: int = CORRECTED_SMOKE_TRANSPORT_RETRIES
    backoff_base_seconds: float = 0.5
    concurrency: int = 8
    timeout_seconds: float = 180.0
    temperature: float = 0.0
    api_seed: int = 0
    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = None
    resume: bool = True
    use_cache: bool = True
    protocol_version: str = COMPACT_PROTOCOL_VERSION
    runner_version: str = COMPACT_RUNNER_VERSION
    manifest_split: str = "corrected_smoke"
    expected_tasks: int = 12
    expected_attempts: int = 96
    run_id_prefix: str = "coordcap11_"
    formal_safety: bool = False
    cost_limit_usd: float | None = None
    call_cost_reservation_usd: float = 0.04
    progress_interval: int | None = None
    progress_path: Path | None = None
    first_batch_json_validity_min: float = 0.95
    terminal_failure_streak_limit: int = 20

    def __post_init__(self) -> None:
        for field_name in (
            "public_manifest_path",
            "raw_root",
            "parsed_root",
            "cache_root",
            "audit_log",
            "expected_ledger_path",
        ):
            setattr(self, field_name, Path(getattr(self, field_name)))
        if self.progress_path is not None:
            self.progress_path = Path(self.progress_path)
        self.models = tuple(dict.fromkeys(str(item) for item in self.models))
        self.methods = tuple(dict.fromkeys(str(item) for item in self.methods))
        if self.models != CORRECTED_SMOKE_MODELS:
            raise ValueError("compact protocol requires both frozen model routes in order")
        if self.methods != METHODS:
            raise ValueError("compact protocol requires all four methods in order")
        if self.max_output_tokens != 512 or self.primary_cap + self.repair_cap != 512:
            raise ValueError("compact protocol requires a 448+64=512 output-token budget")
        if self.max_semantic_calls != 2 or self.transport_retries != 3:
            raise ValueError("compact protocol requires two semantic calls and three retries")
        if self.concurrency != 8 or self.timeout_seconds != 180.0:
            raise ValueError("compact protocol requires concurrency=8 and timeout=180")
        if self.temperature != 0.0 or self.api_seed != 0:
            raise ValueError("compact protocol requires temperature=0 and seed=0")
        if self.base_url != DEFAULT_BASE_URL or not self.resume or not self.use_cache:
            raise ValueError("compact protocol requires frozen endpoint, resume, and cache")
        if self.protocol_version == COMPACT_PROTOCOL_VERSION:
            if (
                self.runner_version != COMPACT_RUNNER_VERSION
                or self.manifest_split != "corrected_smoke"
                or self.expected_tasks != 12
                or self.expected_attempts != 96
                or self.run_id_prefix != "coordcap11_"
                or self.formal_safety
            ):
                raise ValueError("corrected-smoke execution identity changed")
        elif self.protocol_version == FORMAL_MAIN_PROTOCOL_VERSION:
            if (
                self.runner_version != FORMAL_MAIN_RUNNER_VERSION
                or self.manifest_split != "formal_minimal_main"
                or self.expected_tasks != 80
                or self.expected_attempts != 640
                or self.run_id_prefix != "coordcap12_"
                or not self.formal_safety
                or self.cost_limit_usd != 5.0
                or self.call_cost_reservation_usd != 0.04
                or self.progress_interval != 80
                or self.progress_path is None
                or self.first_batch_json_validity_min != 0.95
                or self.terminal_failure_streak_limit != 20
            ):
                raise ValueError("formal-main execution identity or safety policy changed")
        else:
            raise ValueError("unsupported compact protocol version")


def _sampling(model: str, config: CompactRunnerConfig) -> dict[str, Any]:
    value: dict[str, Any] = {"seed": config.api_seed}
    if model != "openai/gpt-5.4-mini":
        value["temperature"] = config.temperature
    return value


def compact_execution_config_sha256(config: CompactRunnerConfig) -> str:
    payload: dict[str, Any] = {
            "runner_version": config.runner_version,
            "protocol_version": config.protocol_version,
            "base_url": config.base_url,
            "models": list(config.models),
            "methods": list(config.methods),
            "per_model_sampling": {
                model: _sampling(model, config) for model in config.models
            },
            "max_output_tokens": config.max_output_tokens,
            "primary_cap": config.primary_cap,
            "repair_cap": config.repair_cap,
            "max_semantic_calls": config.max_semantic_calls,
            "transport_retries": config.transport_retries,
            "backoff_base_seconds": config.backoff_base_seconds,
            "provider_require_parameters": True,
            "concurrency": config.concurrency,
            "timeout_seconds": config.timeout_seconds,
            "resume": config.resume,
            "use_cache": config.use_cache,
            "compact_schema_version": COMPACT_SCHEMA_VERSION,
            "compact_system": COMPACT_SYSTEM,
            "method_instructions": COMPACT_METHOD_INSTRUCTIONS,
        }
    if config.formal_safety:
        payload["formal_scope_and_safety"] = {
            "manifest_split": config.manifest_split,
            "expected_tasks": config.expected_tasks,
            "expected_attempts": config.expected_attempts,
            "run_id_prefix": config.run_id_prefix,
            "cost_limit_usd": config.cost_limit_usd,
            "call_cost_reservation_usd": config.call_cost_reservation_usd,
            "progress_interval": config.progress_interval,
            "first_batch_json_validity_min": config.first_batch_json_validity_min,
            "terminal_failure_streak_limit": config.terminal_failure_streak_limit,
        }
    return canonical_sha256(payload)


def load_compact_manifest(
    path: Path, config: CompactRunnerConfig
) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    value, error = strict_json_loads(raw.decode("utf-8"))
    if error is not None or not isinstance(value, dict):
        raise ValueError(f"invalid compact public manifest: {error}")
    if value.get("protocol_version") != config.protocol_version:
        raise ValueError("wrong compact protocol version")
    if (
        value.get("split") != config.manifest_split
        or value.get("instance_count") != config.expected_tasks
    ):
        raise ValueError("compact manifest split/task count mismatch")
    instances = value.get("instances")
    if not isinstance(instances, list) or len(instances) != config.expected_tasks:
        raise ValueError("compact manifest instance count mismatch")
    for instance in instances:
        if canonical_sha256(instance["public_task"]) != instance.get("public_sha256"):
            raise ValueError(f"public hash mismatch: {instance.get('instance_id')}")
    return value, hashlib.sha256(raw).hexdigest()


def _run_id(
    *,
    protocol_version: str,
    manifest_sha256: str,
    instance_id: str,
    model: str,
    method: str,
    max_output_tokens: int,
    max_semantic_calls: int,
    prefix: str = "coordcap11_",
) -> str:
    return prefix + canonical_sha256(
        {
            "protocol_version": protocol_version,
            "public_manifest_sha256": manifest_sha256,
            "instance_id": instance_id,
            "model": model,
            "method": method,
            "max_output_tokens": max_output_tokens,
            "max_semantic_calls": max_semantic_calls,
        }
    )[:24]


def build_compact_specs(config: CompactRunnerConfig) -> tuple[list[CompactRunSpec], str]:
    manifest, manifest_sha = load_compact_manifest(config.public_manifest_path, config)
    specs: list[CompactRunSpec] = []
    for instance in manifest["instances"]:
        for model in config.models:
            for method in config.methods:
                identity = {
                    "protocol_version": config.protocol_version,
                    "manifest_sha256": manifest_sha,
                    "instance_id": str(instance["instance_id"]),
                    "model": model,
                    "method": method,
                    "max_output_tokens": config.max_output_tokens,
                    "max_semantic_calls": config.max_semantic_calls,
                }
                specs.append(
                    CompactRunSpec(
                        protocol_version=config.protocol_version,
                        public_manifest_sha256=manifest_sha,
                        run_id=_run_id(**identity, prefix=config.run_id_prefix),
                        instance_id=identity["instance_id"],
                        model=model,
                        method=method,
                        max_output_tokens=config.max_output_tokens,
                        max_semantic_calls=config.max_semantic_calls,
                        instance=instance,
                    )
                )
    if (
        len(specs) != config.expected_attempts
        or len({spec.run_id for spec in specs}) != config.expected_attempts
    ):
        raise ValueError("compact ledger attempt count mismatch")
    return specs, manifest_sha


def build_compact_ledger(config: CompactRunnerConfig) -> dict[str, Any]:
    specs, manifest_sha = build_compact_specs(config)
    return {
        "protocol_version": config.protocol_version,
        "public_manifest_sha256": manifest_sha,
        "execution_config_sha256": compact_execution_config_sha256(config),
        "runs": [spec.ledger_record() for spec in specs],
    }


def write_compact_ledger(config: CompactRunnerConfig) -> dict[str, Any]:
    ledger = build_compact_ledger(config)
    _write_ledger(
        config.expected_ledger_path,
        ledger,
        overwrite=config.overwrite_expected_ledger,
    )
    return ledger


class CompactRunner:
    def __init__(self, config: CompactRunnerConfig, transport: Any | None = None) -> None:
        self.config = config
        self.transport = transport
        self.execution_hash = compact_execution_config_sha256(config)
        self.semaphore = asyncio.Semaphore(config.concurrency)
        self.audit_lock = asyncio.Lock()

    def _ensure_transport(self) -> None:
        if self.transport is not None:
            return
        key = self.config.api_key or os.getenv("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        self.transport = OpenRouterTransport(api_key=key, base_url=self.config.base_url)

    async def _audit(self, value: Mapping[str, Any]) -> None:
        line = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        async with self.audit_lock:
            self.config.audit_log.parent.mkdir(parents=True, exist_ok=True)
            with self.config.audit_log.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def _attempt_path(
        self,
        spec: CompactRunSpec,
        call_index: int,
        stage: str,
        transport_index: int,
    ) -> Path:
        return (
            self.config.raw_root
            / spec.run_id
            / f"call_{call_index:02d}_{stage}_transport_{transport_index:02d}.json"
        )

    def _parsed_path(self, spec: CompactRunSpec) -> Path:
        return self.config.parsed_root / f"{spec.run_id}.json"

    def _normalize_attempt(
        self,
        *,
        spec: CompactRunSpec,
        call_index: int,
        stage: str,
        transport_index: int,
        request_hash: str,
        request_payload: Mapping[str, Any],
        response: Mapping[str, Any],
        source: str,
        latency_seconds: float,
        backoff_seconds: float,
    ) -> dict[str, Any]:
        choices = response.get("choices")
        success = (
            response.get("_coordcap_transport_success") is not False
            and isinstance(choices, list)
            and bool(choices)
            and isinstance(choices[0], Mapping)
        )
        first = choices[0] if success else {}
        finish_reason = str(first.get("finish_reason", ""))
        content = _response_content(response)
        usage = response.get("usage") if isinstance(response.get("usage"), Mapping) else {}
        returned_model = str(response.get("model", ""))
        return {
            "schema_version": "coordcap-compact-transport-attempt-1.0",
            "timestamp_utc": utc_now(),
            "protocol_version": self.config.protocol_version,
            "run_id": spec.run_id,
            "instance_id": spec.instance_id,
            "model": spec.model,
            "method": spec.method,
            "call_index": call_index,
            "stage": stage,
            "transport_index": transport_index,
            "backoff_seconds_before_attempt": backoff_seconds,
            "request_hash": request_hash,
            "request_payload": dict(request_payload),
            "response_payload": dict(response),
            "response_payload_sha256": response_payload_sha256(response),
            "source": source,
            "transport_success": success,
            "retryable": bool(response.get("retryable", False)),
            "status_code": response.get("_coordcap_status_code", response.get("status_code")),
            "error": None if success else str(response.get("error", "missing choices")),
            "returned_model": returned_model,
            "returned_provider": str(response.get("provider", "")),
            "route_consistency_rule": ROUTE_CONSISTENCY_RULE,
            "route_consistent": success and returned_model == spec.model,
            "finish_reason": finish_reason,
            "truncated": finish_reason == "length",
            "max_output_tokens": request_payload.get("max_tokens"),
            "raw_content": content,
            "response_content_chars": len(content),
            "response_content_bytes": len(content.encode("utf-8")),
            "usage": dict(usage),
            "reported_cost": _optional_float(usage.get("cost")),
            "latency_seconds": latency_seconds,
        }

    def _verify_attempt(
        self,
        path: Path,
        *,
        spec: CompactRunSpec,
        expected_request_hash: str,
    ) -> dict[str, Any]:
        record = _load_object(path, "compact raw attempt")
        if record.get("run_id") != spec.run_id:
            raise RuntimeError(f"raw run mismatch: {path}")
        payload = record.get("request_payload")
        response = record.get("response_payload")
        if not isinstance(payload, Mapping) or not isinstance(response, Mapping):
            raise RuntimeError(f"raw payload missing: {path}")
        if _request_hash(self.config.base_url, payload) != expected_request_hash:
            raise RuntimeError(f"raw request hash mismatch: {path}")
        if record.get("request_hash") != expected_request_hash:
            raise RuntimeError(f"stored raw request hash mismatch: {path}")
        if record.get("response_payload_sha256") != response_payload_sha256(response):
            raise RuntimeError(f"raw response hash mismatch: {path}")
        return record

    def _call_summary(self, records: Sequence[Mapping[str, Any]], paths: Sequence[Path]) -> dict[str, Any]:
        final = records[-1]
        usage = final.get("usage") if isinstance(final.get("usage"), Mapping) else {}
        prompt_tokens = _usage_int(usage.get("prompt_tokens"))
        completion_tokens = _usage_int(usage.get("completion_tokens"))
        total_tokens = _usage_int(usage.get("total_tokens"))
        return {
            "call_index": final["call_index"],
            "stage": final["stage"],
            "request_hash": final["request_hash"],
            "transport_attempt_paths": [str(path) for path in paths],
            "transport_attempt_count": len(paths),
            "transport_retry_count": max(0, len(paths) - 1),
            "transport_success": final.get("transport_success") is True,
            "retryable": bool(final.get("retryable")),
            "error": final.get("error"),
            "returned_model": final.get("returned_model"),
            "returned_provider": final.get("returned_provider"),
            "route_consistent": final.get("route_consistent") is True,
            "finish_reason": final.get("finish_reason"),
            "truncated": final.get("truncated") is True,
            "max_output_tokens": final.get("max_output_tokens"),
            "raw_content": final.get("raw_content", ""),
            "response_content_chars": final.get("response_content_chars", 0),
            "response_content_bytes": final.get("response_content_bytes", 0),
            "usage_complete": all(
                value is not None for value in (prompt_tokens, completion_tokens, total_tokens)
            ),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "reported_cost": _optional_float(final.get("reported_cost")),
            "latency_seconds": sum(float(row.get("latency_seconds", 0.0)) for row in records),
        }

    async def _logical_call(
        self,
        *,
        spec: CompactRunSpec,
        call_index: int,
        stage: str,
        messages: Sequence[Mapping[str, str]],
        max_tokens: int,
    ) -> dict[str, Any]:
        public_task = spec.instance["public_task"]
        schema = compact_schema_for_task(public_task)
        payload: dict[str, Any] = {
            "model": spec.model,
            "messages": [dict(message) for message in messages],
            "max_tokens": max_tokens,
            "response_format": response_format(schema, name="coordcap_compact_decision"),
            "provider": {"require_parameters": True},
        }
        payload.update(_sampling(spec.model, self.config))
        payload_text = canonical_json(payload).lower()
        leaked = [term for term in FORBIDDEN_REQUEST_TERMS if term in payload_text]
        if leaked:
            raise RuntimeError(f"formal request leakage canary fired: {leaked}")
        request_hash = _request_hash(self.config.base_url, payload)
        records: list[dict[str, Any]] = []
        paths: list[Path] = []

        for transport_index in range(self.config.transport_retries + 1):
            path = self._attempt_path(spec, call_index, stage, transport_index)
            if not path.exists():
                break
            record = self._verify_attempt(
                path,
                spec=spec,
                expected_request_hash=request_hash,
            )
            records.append(record)
            paths.append(path)
            if record.get("transport_success") is True or record.get("retryable") is not True:
                return self._call_summary(records, paths)

        next_index = len(records)
        if next_index > self.config.transport_retries:
            return self._call_summary(records, paths)

        cache_path = self.config.cache_root / f"{request_hash}.json"
        if not records and self.config.use_cache and cache_path.exists():
            cache = _load_object(cache_path, "compact cache")
            response = cache.get("response_payload")
            if cache.get("request_hash") != request_hash or not isinstance(response, Mapping):
                raise RuntimeError(f"cache identity mismatch: {cache_path}")
            if cache.get("response_payload_sha256") != response_payload_sha256(response):
                raise RuntimeError(f"cache response hash mismatch: {cache_path}")
            record = self._normalize_attempt(
                spec=spec,
                call_index=call_index,
                stage=stage,
                transport_index=0,
                request_hash=request_hash,
                request_payload=payload,
                response=response,
                source="cache",
                latency_seconds=0.0,
                backoff_seconds=0.0,
            )
            path = self._attempt_path(spec, call_index, stage, 0)
            _write_exclusive(path, record)
            records.append(record)
            paths.append(path)
            await self._audit(
                {
                    "event": "transport_attempt",
                    "timestamp_utc": utc_now(),
                    "run_id": spec.run_id,
                    "call_index": call_index,
                    "stage": stage,
                    "transport_index": 0,
                    "source": "cache",
                    "transport_success": True,
                    "raw_path": str(path),
                }
            )
            return self._call_summary(records, paths)

        self._ensure_transport()
        for transport_index in range(next_index, self.config.transport_retries + 1):
            delay = 0.0
            if transport_index > 0:
                delay = self.config.backoff_base_seconds * (2 ** (transport_index - 1))
                delay += _deterministic_jitter(spec.run_id, call_index, transport_index)
                await self._audit(
                    {
                        "event": "transport_retry_scheduled",
                        "timestamp_utc": utc_now(),
                        "run_id": spec.run_id,
                        "call_index": call_index,
                        "stage": stage,
                        "transport_index": transport_index,
                        "backoff_seconds": delay,
                    }
                )
                await asyncio.sleep(delay)
            started = time.perf_counter()
            try:
                async with self.semaphore:
                    response = await self.transport.request(
                        payload=payload,
                        timeout_seconds=self.config.timeout_seconds,
                    )
            except Exception as exc:
                response = {
                    "_coordcap_transport_success": False,
                    "status_code": None,
                    "error": f"{type(exc).__name__}: {exc}"[:4000],
                    "retryable": True,
                }
            record = self._normalize_attempt(
                spec=spec,
                call_index=call_index,
                stage=stage,
                transport_index=transport_index,
                request_hash=request_hash,
                request_payload=payload,
                response=response,
                source="network",
                latency_seconds=time.perf_counter() - started,
                backoff_seconds=delay,
            )
            path = self._attempt_path(spec, call_index, stage, transport_index)
            _write_exclusive(path, record)
            records.append(record)
            paths.append(path)
            await self._audit(
                {
                    "event": "transport_attempt",
                    "timestamp_utc": utc_now(),
                    "run_id": spec.run_id,
                    "call_index": call_index,
                    "stage": stage,
                    "transport_index": transport_index,
                    "backoff_seconds": delay,
                    "source": "network",
                    "transport_success": record["transport_success"],
                    "retryable": record["retryable"],
                    "route_consistent": record["route_consistent"],
                    "finish_reason": record["finish_reason"],
                    "truncated": record["truncated"],
                    "max_output_tokens": record["max_output_tokens"],
                    "response_content_chars": record["response_content_chars"],
                    "response_content_bytes": record["response_content_bytes"],
                    "reported_cost": record["reported_cost"],
                    "latency_seconds": record["latency_seconds"],
                    "raw_path": str(path),
                    "error": record["error"],
                }
            )
            if record["transport_success"]:
                cache = {
                    "schema_version": "coordcap-compact-cache-1.0",
                    "request_hash": request_hash,
                    "response_payload": dict(response),
                    "response_payload_sha256": response_payload_sha256(response),
                }
                if self.config.use_cache and not cache_path.exists():
                    try:
                        _write_exclusive(cache_path, cache)
                    except FileExistsError:
                        pass
                break
            if not record["retryable"]:
                break
        return self._call_summary(records, paths)

    async def _execute(self, spec: CompactRunSpec) -> tuple[dict[str, Any], bool]:
        parsed_path = self._parsed_path(spec)
        if parsed_path.exists():
            if not self.config.resume:
                raise FileExistsError(f"parsed terminal exists: {parsed_path}")
            terminal = _load_object(parsed_path, "compact parsed terminal")
            if terminal.get("run_id") != spec.run_id:
                raise RuntimeError(f"parsed run mismatch: {parsed_path}")
            if terminal.get("execution_config_sha256") != self.execution_hash:
                raise RuntimeError(f"parsed config mismatch: {parsed_path}")
            referenced_paths: list[str] = []
            calls = terminal.get("calls")
            if not isinstance(calls, list):
                raise RuntimeError(f"parsed calls missing: {parsed_path}")
            for call in calls:
                if not isinstance(call, Mapping) or not isinstance(call.get("request_hash"), str):
                    raise RuntimeError(f"parsed call binding missing: {parsed_path}")
                paths = call.get("transport_attempt_paths")
                if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
                    raise RuntimeError(f"parsed transport paths invalid: {parsed_path}")
                referenced_paths.extend(paths)
                for raw_path in paths:
                    self._verify_attempt(
                        Path(raw_path),
                        spec=spec,
                        expected_request_hash=str(call["request_hash"]),
                    )
            if referenced_paths != terminal.get("raw_paths"):
                raise RuntimeError(f"parsed flattened raw paths disagree: {parsed_path}")
            return terminal, True

        public_task = spec.instance["public_task"]
        metadata = {
            "instance_id": spec.instance_id,
            "task_family": spec.instance["task_family"],
            "principal_count": spec.instance["principal_count"],
            "conflict_level": spec.instance["conflict_level"],
            "public_sha256": spec.instance["public_sha256"],
        }
        initial = await self._logical_call(
            spec=spec,
            call_index=0,
            stage="decision",
            messages=build_compact_messages(
                method=spec.method,
                public_task=public_task,
                metadata=metadata,
            ),
            max_tokens=self.config.primary_cap,
        )
        calls = [initial]
        initial_value: dict[str, Any] | None = None
        initial_valid = False
        initial_errors: list[str] = []
        if initial["transport_success"]:
            initial_value, initial_valid, initial_errors = parse_compact_response(
                str(initial["raw_content"]), public_task
            )
        else:
            initial_errors = [f"transport_error: {initial.get('error')}"]

        effective_value = initial_value
        effective_valid = initial_valid
        effective_errors = list(initial_errors)
        repair_used = False
        repair_decision_preserved: bool | None = None
        marker = extract_compact_marker(str(initial["raw_content"]))
        initial_completion = initial.get("completion_tokens")
        can_repair = (
            initial["transport_success"]
            and not initial_valid
            and not initial["truncated"]
            and marker is not None
            and isinstance(initial_completion, int)
            and initial_completion + self.config.repair_cap <= self.config.max_output_tokens
        )
        if can_repair:
            repair_used = True
            repair = await self._logical_call(
                spec=spec,
                call_index=1,
                stage="serialization_repair",
                messages=build_compact_repair_messages(
                    marker=marker,
                    invalid_text=str(initial["raw_content"]),
                ),
                max_tokens=self.config.repair_cap,
            )
            calls.append(repair)
            if repair["transport_success"]:
                repaired, repaired_valid, repaired_errors = parse_compact_response(
                    str(repair["raw_content"]), public_task
                )
                repaired_marker = (
                    (repaired.get("plan_id"), bool(repaired.get("abstain")))
                    if isinstance(repaired, Mapping)
                    else None
                )
                repair_decision_preserved = repaired_marker == marker
                effective_value = repaired
                effective_valid = repaired_valid and repair_decision_preserved
                effective_errors = list(repaired_errors)
                if repaired_valid and not repair_decision_preserved:
                    effective_errors.append("repair_changed_decision")
            else:
                effective_valid = False
                effective_errors = [f"repair_transport_error: {repair.get('error')}"]

        terminal_transport_failure = any(call["transport_success"] is not True for call in calls)
        route_consistent = all(
            call["transport_success"] is True and call["route_consistent"] is True
            for call in calls
        )
        truncated_output = bool(initial["truncated"] and not initial_valid)
        if terminal_transport_failure:
            status = "terminal_transport_failure"
        elif not route_consistent:
            status = "route_mismatch"
        elif effective_valid:
            status = "complete"
        elif truncated_output:
            status = "truncated_output"
        else:
            status = "invalid_output"

        decoded = (
            decode_compact_decision(effective_value, public_task)
            if effective_valid and isinstance(effective_value, Mapping)
            else None
        )
        successful_calls = [call for call in calls if call["transport_success"]]
        usage_complete = bool(successful_calls) and all(
            call["usage_complete"] for call in successful_calls
        )
        completion_values = [
            call["completion_tokens"]
            for call in calls
            if call["transport_success"] and isinstance(call["completion_tokens"], int)
        ]
        total_completion = sum(completion_values) if usage_complete else None
        budget_compliant = (
            total_completion <= self.config.max_output_tokens
            if total_completion is not None
            else None
        )
        raw_paths = [
            path
            for call in calls
            for path in call["transport_attempt_paths"]
        ]
        terminal = {
            "schema_version": "coordcap-compact-terminal-1.0",
            "created_at_utc": utc_now(),
            "runner_version": self.config.runner_version,
            "protocol_version": self.config.protocol_version,
            "public_manifest_sha256": spec.public_manifest_sha256,
            "execution_config_sha256": self.execution_hash,
            "public_sha256": spec.instance["public_sha256"],
            **spec.ledger_record(),
            "task_family": spec.instance["task_family"],
            "principal_count": spec.instance["principal_count"],
            "conflict_level": spec.instance["conflict_level"],
            "compact_schema_sha256": canonical_sha256(compact_schema_for_task(public_task)),
            "status": status,
            "initial_json_valid": initial_valid,
            "effective_json_valid": effective_valid,
            "initial_validation_errors": initial_errors,
            "effective_validation_errors": effective_errors,
            "initial_compact_output": initial_value,
            "compact_output": effective_value,
            "decoded_decision": decoded,
            "truncated_output": truncated_output,
            "repair_used": repair_used,
            "repair_decision_preserved": repair_decision_preserved,
            "semantic_scorable": effective_valid,
            "terminal_transport_failure": terminal_transport_failure,
            "route_consistent": route_consistent,
            "usage_complete": usage_complete,
            "budget_compliant": budget_compliant,
            "total_completion_tokens": total_completion,
            "total_prompt_tokens": (
                sum(int(call["prompt_tokens"]) for call in calls)
                if all(isinstance(call["prompt_tokens"], int) for call in calls)
                else None
            ),
            "total_tokens": (
                sum(int(call["total_tokens"]) for call in calls)
                if all(isinstance(call["total_tokens"], int) for call in calls)
                else None
            ),
            "reported_cost": (
                sum(float(call["reported_cost"]) for call in calls)
                if all(call["reported_cost"] is not None for call in calls)
                else None
            ),
            "reported_cost_partial": sum(
                float(call["reported_cost"] or 0.0) for call in calls
            ),
            "latency_seconds": sum(float(call["latency_seconds"]) for call in calls),
            "semantic_call_count": len(calls),
            "network_attempt_count": sum(call["transport_attempt_count"] for call in calls),
            "transport_retry_count": sum(call["transport_retry_count"] for call in calls),
            "calls": [
                {key: value for key, value in call.items() if key != "raw_content"}
                for call in calls
            ],
            "raw_paths": raw_paths,
        }
        _write_exclusive(parsed_path, terminal)
        await self._audit(
            {
                "event": "terminal_record",
                "timestamp_utc": utc_now(),
                "run_id": spec.run_id,
                "model": spec.model,
                "method": spec.method,
                "status": status,
                "initial_json_valid": initial_valid,
                "effective_json_valid": effective_valid,
                "truncated_output": truncated_output,
                "repair_used": repair_used,
                "terminal_transport_failure": terminal_transport_failure,
                "semantic_call_count": len(calls),
                "network_attempt_count": terminal["network_attempt_count"],
                "transport_retry_count": terminal["transport_retry_count"],
                "reported_cost": terminal["reported_cost"],
                "latency_seconds": terminal["latency_seconds"],
                "parsed_path": str(parsed_path),
            }
        )
        return terminal, False

    async def run(self) -> dict[str, Any]:
        ledger = write_compact_ledger(self.config)
        specs, manifest_sha = build_compact_specs(self.config)
        if ledger["public_manifest_sha256"] != manifest_sha:
            raise RuntimeError("ledger/manifest hash disagreement")
        self._ensure_transport()

        async def one(spec: CompactRunSpec) -> tuple[dict[str, Any], bool]:
            return await self._execute(spec)

        results = await asyncio.gather(*(one(spec) for spec in specs))
        terminals = [row for row, _ in results]
        resumed = sum(was_resumed for _, was_resumed in results)
        return {
            "protocol_version": self.config.protocol_version,
            "public_manifest_sha256": manifest_sha,
            "execution_config_sha256": self.execution_hash,
            "expected_runs": len(specs),
            "terminal_records": len(terminals),
            "resumed_records": resumed,
            "status_counts": dict(sorted(Counter(row["status"] for row in terminals).items())),
            "network_attempts": sum(int(row["network_attempt_count"]) for row in terminals),
            "transport_retries": sum(int(row["transport_retry_count"]) for row in terminals),
        }


class FormalMainRunner(CompactRunner):
    """Batching/safety wrapper for the frozen 640-attempt formal main matrix."""

    def _provider_failure_streaks(
        self, terminals: Sequence[Mapping[str, Any]]
    ) -> dict[str, int]:
        streaks = {model: 0 for model in self.config.models}
        maximums = {model: 0 for model in self.config.models}
        for terminal in terminals:
            model = str(terminal["model"])
            if terminal.get("terminal_transport_failure") is True:
                streaks[model] += 1
                maximums[model] = max(maximums[model], streaks[model])
            else:
                streaks[model] = 0
        return maximums

    async def run(self) -> dict[str, Any]:
        if not self.config.formal_safety:
            raise RuntimeError("formal runner requires frozen safety policy")
        ledger = write_compact_ledger(self.config)
        specs, manifest_sha = build_compact_specs(self.config)
        if ledger["public_manifest_sha256"] != manifest_sha:
            raise RuntimeError("ledger/manifest hash disagreement")
        self._ensure_transport()

        progress_document: dict[str, Any] = {
            "schema_version": "coordcap-formal-main-progress-1.0",
            "protocol_version": self.config.protocol_version,
            "execution_config_sha256": self.execution_hash,
            "expected_attempts": self.config.expected_attempts,
            "history": [],
        }
        if self.config.progress_path is not None and self.config.progress_path.exists():
            existing = _load_object(self.config.progress_path, "formal progress")
            if existing.get("execution_config_sha256") != self.execution_hash:
                raise RuntimeError("formal progress execution binding mismatch")
            progress_document = existing
        existing_checkpoints = {
            int(row["completed_attempts"])
            for row in progress_document.get("history", [])
            if isinstance(row, Mapping) and isinstance(row.get("completed_attempts"), int)
        }

        terminals: list[dict[str, Any]] = []
        resumed = 0
        safe_stop_reason: str | None = None
        started = time.perf_counter()
        chunk_size = self.config.concurrency
        for offset in range(0, len(specs), chunk_size):
            chunk = specs[offset : offset + chunk_size]
            current_cost = sum(float(row.get("reported_cost_partial") or 0.0) for row in terminals)
            network_candidates = sum(not self._parsed_path(spec).exists() for spec in chunk)
            reserve = (
                network_candidates
                * self.config.max_semantic_calls
                * self.config.call_cost_reservation_usd
            )
            if (
                self.config.cost_limit_usd is not None
                and current_cost + reserve > self.config.cost_limit_usd
            ):
                safe_stop_reason = "cost_reservation_would_exceed_hard_limit"
                break

            chunk_results = await asyncio.gather(*(self._execute(spec) for spec in chunk))
            terminals.extend(row for row, _ in chunk_results)
            resumed += sum(was_resumed for _, was_resumed in chunk_results)

            for terminal in terminals[-len(chunk) :]:
                if terminal.get("status") == "complete" and not isinstance(
                    terminal.get("decoded_decision"), Mapping
                ):
                    safe_stop_reason = "evaluator_invariant_failure"
                    break
            if safe_stop_reason:
                break

            completed = len(terminals)
            cost = sum(float(row.get("reported_cost_partial") or 0.0) for row in terminals)
            if self.config.cost_limit_usd is not None and cost >= self.config.cost_limit_usd:
                safe_stop_reason = "actual_cost_hard_limit_reached"

            streaks = self._provider_failure_streaks(terminals)
            if any(
                value >= self.config.terminal_failure_streak_limit
                for value in streaks.values()
            ):
                safe_stop_reason = "provider_terminal_transport_failure_streak"

            effective_valid = sum(
                row.get("effective_json_valid") is True for row in terminals
            )
            if completed == self.config.progress_interval:
                if effective_valid / completed < self.config.first_batch_json_validity_min:
                    safe_stop_reason = "first_80_json_validity_below_95pct"

            if (
                self.config.progress_interval is not None
                and completed % self.config.progress_interval == 0
                and completed not in existing_checkpoints
            ):
                elapsed = time.perf_counter() - started
                eta = (
                    elapsed / completed * (self.config.expected_attempts - completed)
                    if completed
                    else None
                )
                progress = {
                    "completed_attempts": completed,
                    "cumulative_reported_cost_usd": cost,
                    "effective_json_validity_rate": effective_valid / completed,
                    "terminal_transport_failures": sum(
                        row.get("terminal_transport_failure") is True
                        for row in terminals
                    ),
                    "estimated_remaining_seconds": eta,
                    "maximum_provider_failure_streak": streaks,
                    "safe_stop_reason": safe_stop_reason,
                    "timestamp_utc": utc_now(),
                }
                progress_document.setdefault("history", []).append(progress)
                assert self.config.progress_path is not None
                self.config.progress_path.parent.mkdir(parents=True, exist_ok=True)
                self.config.progress_path.write_text(
                    json.dumps(progress_document, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                await self._audit({"event": "formal_progress", **progress})
                print(json.dumps({"formal_progress": progress}, sort_keys=True), flush=True)
            if safe_stop_reason:
                await self._audit(
                    {
                        "event": "formal_safe_stop",
                        "timestamp_utc": utc_now(),
                        "reason": safe_stop_reason,
                        "completed_attempts": completed,
                        "cumulative_reported_cost_usd": cost,
                    }
                )
                break

        return {
            "protocol_version": self.config.protocol_version,
            "public_manifest_sha256": manifest_sha,
            "execution_config_sha256": self.execution_hash,
            "expected_runs": len(specs),
            "terminal_records": len(terminals),
            "resumed_records": resumed,
            "status_counts": dict(sorted(Counter(row["status"] for row in terminals).items())),
            "network_attempts": sum(int(row["network_attempt_count"]) for row in terminals),
            "transport_retries": sum(int(row["transport_retry_count"]) for row in terminals),
            "reported_cost_usd": sum(
                float(row.get("reported_cost_partial") or 0.0) for row in terminals
            ),
            "safe_stop_reason": safe_stop_reason,
        }


async def run_compact_corrected_smoke(
    config: CompactRunnerConfig,
    *,
    transport: Any | None = None,
) -> dict[str, Any]:
    return await CompactRunner(config, transport=transport).run()


async def run_formal_main(
    config: CompactRunnerConfig,
    *,
    transport: Any | None = None,
) -> dict[str, Any]:
    return await FormalMainRunner(config, transport=transport).run()
