"""Append-only Phase 3B recovery for the frozen Phase 3A formal matrix."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from .canonical import canonical_json, canonical_sha256
from .compact_runner import CompactRunSpec, CompactRunner, CompactRunnerConfig, build_compact_specs
from .runner import DEFAULT_BASE_URL, OpenRouterTransport, response_payload_sha256, utc_now
from .schema import response_format


RECOVERY_SOURCE = "phase3a_http402_recovery"
RECOVERY_SCHEMA_VERSION = "coordcap-formal-recovery-session-1.0"
RECOVERY_COST_LIMIT_USD = 5.0
HEALTH_MODEL = "google/gemini-3.1-flash-lite"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(dict(value), handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")


def _write_current(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _audit(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(value), ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")


def verify_phase3a_snapshot(root: Path, partition: Mapping[str, Any]) -> None:
    snapshot = partition.get("phase3a_output_snapshot")
    if not isinstance(snapshot, Mapping):
        raise ValueError("recovery partition lacks Phase 3A snapshot")
    for group in ("parsed", "raw", "cache"):
        rows = snapshot.get(group)
        if not isinstance(rows, list):
            raise ValueError(f"recovery partition snapshot lacks {group}")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError(f"invalid snapshot row in {group}")
            path = root / str(row["path"])
            if not path.is_file():
                raise ValueError(f"Phase 3A immutable file is missing: {path}")
            if _sha256(path) != row.get("sha256") or path.stat().st_size != row.get("bytes"):
                raise ValueError(f"Phase 3A immutable file changed: {path}")


def recovery_record_id(recovery_run_id: str, original_run_id: str) -> str:
    return "coordcap3b_" + canonical_sha256(
        {
            "recovery_run_id": recovery_run_id,
            "recovery_source": RECOVERY_SOURCE,
            "original_run_id": original_run_id,
        }
    )[:24]


def load_or_create_session(
    *,
    root: Path,
    partition_path: Path,
    session_path: Path,
) -> dict[str, Any]:
    partition_sha = _sha256(partition_path)
    partition = _load(partition_path)
    recovery_run_id = "coordcap3b_recovery_" + canonical_sha256(
        {
            "partition_sha256": partition_sha,
            "protocol_version": partition["protocol_version"],
            "source": RECOVERY_SOURCE,
        }
    )[:20]
    if session_path.exists():
        session = _load(session_path)
        if session.get("recovery_run_id") != recovery_run_id:
            raise ValueError("existing recovery session identity mismatch")
        if session.get("partition_sha256") != partition_sha:
            raise ValueError("existing recovery session partition mismatch")
        return session
    session = {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "protocol_version": partition["protocol_version"],
        "public_manifest_sha256": partition["public_manifest_sha256"],
        "execution_config_sha256": partition["execution_config_sha256"],
        "partition_sha256": partition_sha,
        "recovery_run_id": recovery_run_id,
        "recovery_started_at": utc_now(),
        "recovery_source": RECOVERY_SOURCE,
        "recovery_target_count": partition["partition_counts"]["recovery_targets"],
        "new_api_cost_hard_limit_usd": RECOVERY_COST_LIMIT_USD,
        "targeted_budget_ablation_authorized": False,
    }
    _write_exclusive(session_path, session)
    return session


async def run_health_check(
    *,
    root: Path,
    session: Mapping[str, Any],
    health_path: Path,
    audit_path: Path,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    """Run at most one tiny matrix-external provider request, with immutable output."""

    if health_path.exists():
        health = _load(health_path)
        if health.get("recovery_run_id") != session.get("recovery_run_id"):
            raise ValueError("health-check recovery binding mismatch")
        _audit(
            audit_path,
            {
                "event": "health_check_resumed",
                "timestamp_utc": utc_now(),
                "recovery_run_id": session["recovery_run_id"],
                "health_status": health.get("status"),
                "network_attempts": 0,
            },
        )
        return health

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean", "const": True}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    payload: dict[str, Any] = {
        "model": HEALTH_MODEL,
        "messages": [
            {"role": "system", "content": "Return only the requested JSON object."},
            {"role": "user", "content": "Return exactly: {\"ok\":true}"},
        ],
        "max_tokens": 16,
        "temperature": 0.0,
        "seed": 0,
        "response_format": response_format(schema, name="coordcap_provider_health"),
        "provider": {"require_parameters": True},
    }
    request_hash = canonical_sha256({"base_url": DEFAULT_BASE_URL, "payload": payload})
    transport = OpenRouterTransport(api_key=api_key, base_url=DEFAULT_BASE_URL)
    started = time.perf_counter()
    response = dict(await transport.request(payload=payload, timeout_seconds=timeout_seconds))
    latency = time.perf_counter() - started
    choices = response.get("choices")
    success = (
        response.get("_coordcap_transport_success") is not False
        and isinstance(choices, list)
        and bool(choices)
        and isinstance(choices[0], Mapping)
    )
    content = ""
    finish_reason = ""
    if success:
        choice = choices[0]
        finish_reason = str(choice.get("finish_reason", ""))
        message = choice.get("message")
        if isinstance(message, Mapping):
            content = str(message.get("content", ""))
    valid = False
    if success:
        try:
            valid = json.loads(content) == {"ok": True}
        except (json.JSONDecodeError, TypeError):
            valid = False
    usage = response.get("usage") if isinstance(response.get("usage"), Mapping) else {}
    cost = usage.get("cost")
    reported_cost = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
    status = "pass" if success and valid and finish_reason != "length" else "fail"
    record = {
        "schema_version": "coordcap-formal-recovery-health-check-1.0",
        "timestamp_utc": utc_now(),
        "recovery_run_id": session["recovery_run_id"],
        "recovery_source": RECOVERY_SOURCE,
        "formal_matrix_attempt": False,
        "model": HEALTH_MODEL,
        "request_hash": request_hash,
        "request_payload": payload,
        "response_payload": response,
        "response_payload_sha256": response_payload_sha256(response),
        "transport_success": success,
        "status_code": response.get("_coordcap_status_code", response.get("status_code")),
        "error": None if success else str(response.get("error", "missing choices")),
        "finish_reason": finish_reason,
        "response_content": content,
        "response_content_chars": len(content),
        "usage": dict(usage),
        "reported_cost": reported_cost,
        "latency_seconds": latency,
        "status": status,
        "network_attempts": 1,
    }
    _write_exclusive(health_path, record)
    _audit(
        audit_path,
        {
            "event": "health_check",
            "timestamp_utc": record["timestamp_utc"],
            "recovery_run_id": session["recovery_run_id"],
            "formal_matrix_attempt": False,
            "model": HEALTH_MODEL,
            "request_hash": request_hash,
            "transport_success": success,
            "status_code": record["status_code"],
            "finish_reason": finish_reason,
            "response_content_chars": len(content),
            "reported_cost": reported_cost,
            "latency_seconds": latency,
            "status": status,
            "health_path": str(health_path),
        },
    )
    return record


def build_recovery_specs(
    *,
    config: CompactRunnerConfig,
    partition: Mapping[str, Any],
    session: Mapping[str, Any],
) -> tuple[list[CompactRunSpec], dict[str, CompactRunSpec]]:
    original_specs, manifest_sha = build_compact_specs(config)
    if manifest_sha != partition.get("public_manifest_sha256"):
        raise ValueError("recovery manifest differs from partition")
    originals = {spec.run_id: spec for spec in original_specs}
    target_ids = partition.get("recovery_target_attempt_ids")
    if not isinstance(target_ids, list) or len(target_ids) != 263 or len(set(target_ids)) != 263:
        raise ValueError("recovery target list is not the frozen 263-attempt partition")
    if set(target_ids) & set(partition.get("complete_attempt_ids", [])):
        raise ValueError("complete Phase 3A attempt entered recovery target set")
    recovery_specs: list[CompactRunSpec] = []
    by_recovery_id: dict[str, CompactRunSpec] = {}
    for original_id in target_ids:
        original = originals.get(str(original_id))
        if original is None:
            raise ValueError(f"unknown recovery target: {original_id}")
        recovery_id = recovery_record_id(str(session["recovery_run_id"]), str(original_id))
        recovered = replace(original, run_id=recovery_id)
        if recovery_id in by_recovery_id:
            raise ValueError(f"recovery ID collision: {recovery_id}")
        recovery_specs.append(recovered)
        by_recovery_id[recovery_id] = original
    return recovery_specs, by_recovery_id


def build_recovery_mapping(
    *,
    root: Path,
    partition: Mapping[str, Any],
    session: Mapping[str, Any],
    recovery_specs: Sequence[CompactRunSpec],
    parsed_root: Path,
) -> dict[str, Any]:
    http402 = set(partition["http402_attempt_ids"])
    original_records = partition["original_terminal_records"]
    rows: list[dict[str, Any]] = []
    target_ids = list(partition["recovery_target_attempt_ids"])
    for original_id, spec in zip(target_ids, recovery_specs, strict=True):
        path = parsed_root / f"{spec.run_id}.json"
        terminal = _load(path) if path.exists() else None
        source_kind = "phase3a_http402" if original_id in http402 else "phase3a_unexecuted"
        old = original_records.get(original_id) if isinstance(original_records, Mapping) else None
        rows.append(
            {
                "original_attempt_id": original_id,
                "source_kind": source_kind,
                "old_record_id": original_id if old is not None else None,
                "old_terminal_path": old.get("terminal_path") if isinstance(old, Mapping) else None,
                "old_terminal_sha256": old.get("terminal_sha256") if isinstance(old, Mapping) else None,
                "recovery_record_id": spec.run_id,
                "recovery_terminal_path": str(path.relative_to(root)),
                "recovery_terminal_sha256": _sha256(path) if path.exists() else None,
                "recovery_status": terminal.get("status") if terminal else "not_executed",
                "recovery_effective_json_valid": terminal.get("effective_json_valid") if terminal else None,
                "recovery_terminal_transport_failure": terminal.get("terminal_transport_failure") if terminal else None,
            }
        )
    return {
        "schema_version": "coordcap-formal-recovery-mapping-1.0",
        "protocol_version": session["protocol_version"],
        "recovery_run_id": session["recovery_run_id"],
        "recovery_started_at": session["recovery_started_at"],
        "recovery_source": RECOVERY_SOURCE,
        "public_manifest_sha256": session["public_manifest_sha256"],
        "partition_sha256": session["partition_sha256"],
        "target_count": len(rows),
        "recovered_terminal_count": sum(row["recovery_status"] != "not_executed" for row in rows),
        "http402_mapping_count": sum(row["source_kind"] == "phase3a_http402" for row in rows),
        "unexecuted_mapping_count": sum(row["source_kind"] == "phase3a_unexecuted" for row in rows),
        "records": rows,
    }


async def run_recovery(
    *,
    root: Path,
    config: CompactRunnerConfig,
    partition_path: Path,
    session: Mapping[str, Any],
    health_path: Path,
    mapping_path: Path,
    progress_path: Path,
) -> dict[str, Any]:
    partition = _load(partition_path)
    verify_phase3a_snapshot(root, partition)
    health = _load(health_path)
    provider_available = (
        health.get("network_attempts") == 1
        and health.get("transport_success") is True
        and health.get("status_code") == 200
        and health.get("model") == HEALTH_MODEL
        and health.get("reported_cost") is not None
    )
    health_assessment = {
        "schema_version": "coordcap-formal-recovery-health-assessment-1.0",
        "timestamp_utc": utc_now(),
        "recovery_run_id": session["recovery_run_id"],
        "health_check_sha256": _sha256(health_path),
        "network_attempts": health.get("network_attempts"),
        "provider_account_availability": "pass" if provider_available else "fail",
        "transport_success": health.get("transport_success"),
        "status_code": health.get("status_code"),
        "billed_usage_present": health.get("reported_cost") is not None,
        "content_conformance": health.get("status"),
        "finish_reason": health.get("finish_reason"),
        "proceed_to_recovery": provider_available,
        "interpretation": (
            "The sole matrix-external check produced an authenticated, billed HTTP 200 response "
            "on the requested route, establishing account/provider availability. Its deliberately "
            "tiny 16-token cap truncated the JSON payload; this content-conformance failure is "
            "retained and is not retried. Formal attempts keep the frozen 448-token primary cap."
        ),
    }
    assessment_path = root / "audits" / "formal_recovery_health_assessment.json"
    if assessment_path.exists():
        existing_assessment = _load(assessment_path)
        if existing_assessment.get("health_check_sha256") != health_assessment["health_check_sha256"]:
            raise RuntimeError("health assessment binding changed")
    else:
        _write_exclusive(assessment_path, health_assessment)
    if not provider_available:
        raise RuntimeError("successful one-call provider availability check is required")
    if health.get("recovery_run_id") != session.get("recovery_run_id"):
        raise RuntimeError("health check belongs to another recovery session")
    health_cost = float(health.get("reported_cost") or 0.0)
    if health_cost >= RECOVERY_COST_LIMIT_USD:
        raise RuntimeError("health check exhausted recovery cost limit")

    recovery_specs, _ = build_recovery_specs(
        config=config,
        partition=partition,
        session=session,
    )
    runner = CompactRunner(config)
    runner._ensure_transport()
    terminals: list[dict[str, Any]] = []
    safe_stop_reason: str | None = None
    started = time.perf_counter()
    checkpoint_values = {80, 160, 240, len(recovery_specs)}
    for offset in range(0, len(recovery_specs), config.concurrency):
        chunk = recovery_specs[offset : offset + config.concurrency]
        current_cost = health_cost + sum(float(row.get("reported_cost_partial") or 0.0) for row in terminals)
        network_candidates = sum(not runner._parsed_path(spec).exists() for spec in chunk)
        reserve = network_candidates * config.max_semantic_calls * config.call_cost_reservation_usd
        if current_cost + reserve > RECOVERY_COST_LIMIT_USD:
            safe_stop_reason = "cost_reservation_would_exceed_recovery_hard_limit"
            break
        chunk_results = await asyncio.gather(*(runner._execute(spec) for spec in chunk))
        terminals.extend(row for row, _ in chunk_results)

        chunk_402 = False
        for terminal in terminals[-len(chunk) :]:
            for raw_name in terminal.get("raw_paths", []):
                raw = _load(Path(str(raw_name)))
                if raw.get("status_code") == 402:
                    chunk_402 = True
                    break
            if chunk_402:
                break
        if chunk_402:
            safe_stop_reason = "http402_recurred_global_safe_stop"

        current_cost = health_cost + sum(float(row.get("reported_cost_partial") or 0.0) for row in terminals)
        if current_cost >= RECOVERY_COST_LIMIT_USD:
            safe_stop_reason = "recovery_actual_cost_hard_limit_reached"
        for terminal in terminals[-len(chunk) :]:
            if terminal.get("status") == "complete" and not isinstance(
                terminal.get("decoded_decision"), Mapping
            ):
                safe_stop_reason = "evaluator_invariant_failure"
                break

        completed = len(terminals)
        mapping = build_recovery_mapping(
            root=root,
            partition=partition,
            session=session,
            recovery_specs=recovery_specs,
            parsed_root=config.parsed_root,
        )
        _write_current(mapping_path, mapping)
        if completed in checkpoint_values or safe_stop_reason:
            elapsed = time.perf_counter() - started
            remaining = len(recovery_specs) - completed
            progress = {
                "schema_version": "coordcap-formal-recovery-progress-1.0",
                "timestamp_utc": utc_now(),
                "protocol_version": session["protocol_version"],
                "recovery_run_id": session["recovery_run_id"],
                "recovery_source": RECOVERY_SOURCE,
                "recovery_target_count": len(recovery_specs),
                "recovery_terminal_records": completed,
                "cumulative_new_cost_usd_including_health": current_cost,
                "matrix_recovery_cost_usd": current_cost - health_cost,
                "health_check_cost_usd": health_cost,
                "effective_json_validity_rate": (
                    sum(row.get("effective_json_valid") is True for row in terminals) / completed
                    if completed
                    else None
                ),
                "terminal_transport_failures": sum(
                    row.get("terminal_transport_failure") is True for row in terminals
                ),
                "http402_observed": chunk_402,
                "estimated_remaining_seconds": elapsed / completed * remaining if completed else None,
                "safe_stop_reason": safe_stop_reason,
            }
            _write_current(progress_path, progress)
            await runner._audit({"event": "formal_recovery_progress", **progress})
            print(json.dumps({"formal_recovery_progress": progress}, sort_keys=True), flush=True)
        if safe_stop_reason:
            await runner._audit(
                {
                    "event": "formal_recovery_safe_stop",
                    "timestamp_utc": utc_now(),
                    "recovery_run_id": session["recovery_run_id"],
                    "reason": safe_stop_reason,
                    "recovery_terminal_records": completed,
                    "cumulative_new_cost_usd_including_health": current_cost,
                }
            )
            break

    verify_phase3a_snapshot(root, partition)
    mapping = build_recovery_mapping(
        root=root,
        partition=partition,
        session=session,
        recovery_specs=recovery_specs,
        parsed_root=config.parsed_root,
    )
    _write_current(mapping_path, mapping)
    summary = {
        "schema_version": "coordcap-formal-recovery-summary-1.0",
        "protocol_version": session["protocol_version"],
        "recovery_run_id": session["recovery_run_id"],
        "recovery_started_at": session["recovery_started_at"],
        "recovery_source": RECOVERY_SOURCE,
        "recovery_targets": len(recovery_specs),
        "recovery_terminal_records": len(terminals),
        "status_counts": dict(sorted(Counter(str(row["status"]) for row in terminals).items())),
        "network_attempts": sum(int(row.get("network_attempt_count", 0)) for row in terminals),
        "transport_retries": sum(int(row.get("transport_retry_count", 0)) for row in terminals),
        "serialization_repairs": sum(row.get("repair_used") is True for row in terminals),
        "health_check_cost_usd": health_cost,
        "matrix_recovery_cost_usd": sum(float(row.get("reported_cost_partial") or 0.0) for row in terminals),
        "new_recovery_cost_usd": health_cost
        + sum(float(row.get("reported_cost_partial") or 0.0) for row in terminals),
        "safe_stop_reason": safe_stop_reason,
        "phase3a_snapshot_unchanged": True,
    }
    await runner._audit({"event": "formal_recovery_summary", "timestamp_utc": utc_now(), **summary})
    return summary
