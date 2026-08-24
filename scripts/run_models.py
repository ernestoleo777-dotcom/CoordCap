#!/usr/bin/env python3
"""Run the frozen CoordCap matrix or write its expected-run ledger."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from coordcap.runner import (  # noqa: E402
    DEFAULT_BASE_URL,
    RunnerConfig,
    run_coordcap,
    write_expected_run_ledger,
)
from coordcap.schema import ANSWER_TOKEN_BUDGETS, CALL_BUDGETS, METHODS  # noqa: E402


def _split(values: list[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        result.extend(item.strip() for item in value.split(",") if item.strip())
    return list(dict.fromkeys(result))


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="public manifest only; oracle/gold manifests are not accepted",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        help="model ids; defaults to COORDCAP_MODELS then OPENROUTER_MODEL",
    )
    parser.add_argument("--methods", nargs="+", choices=METHODS, default=list(METHODS))
    parser.add_argument(
        "--max-answer-tokens",
        nargs="+",
        type=int,
        choices=ANSWER_TOKEN_BUDGETS,
        default=None,
    )
    parser.add_argument(
        "--max-model-calls",
        nargs="+",
        type=int,
        choices=CALL_BUDGETS,
        default=None,
    )
    parser.add_argument(
        "--order-variants",
        nargs="+",
        default=["canonical"],
        help="canonical, reverse, or rotate_N",
    )
    frozen = parser.add_mutually_exclusive_group()
    frozen.add_argument(
        "--frozen-smoke",
        action="store_true",
        help="enforce the frozen 320-run smoke panel and its execution parameters",
    )
    frozen.add_argument(
        "--frozen-formal",
        action="store_true",
        help=(
            "run the 20,736 canonical formal matrix and add only the frozen 192-run "
            "reverse consistency panel"
        ),
    )
    parser.add_argument("--raw-root", type=Path, default=PROJECT_ROOT / "outputs" / "raw")
    parser.add_argument(
        "--parsed-root", type=Path, default=PROJECT_ROOT / "outputs" / "parsed"
    )
    parser.add_argument("--cache-root", type=Path, default=PROJECT_ROOT / "outputs" / "cache")
    parser.add_argument("--audit-log", type=Path)
    parser.add_argument(
        "--expected-ledger",
        type=Path,
        default=PROJECT_ROOT / "outputs" / "expected_runs.json",
    )
    parser.add_argument("--overwrite-expected-ledger", action="store_true")
    parser.add_argument(
        "--ledger-only",
        action="store_true",
        help="write expected_runs.json and exit without requiring an API key",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=_env_float("COORDCAP_TEMPERATURE", 0.0),
    )
    parser.add_argument("--api-seed", type=int, default=_env_int("COORDCAP_API_SEED", 0))
    parser.add_argument(
        "--base-url",
        default=os.getenv("OPENROUTER_BASE_URL", DEFAULT_BASE_URL),
    )
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--no-repair", action="store_true")
    parser.add_argument(
        "--strict-exit",
        action="store_true",
        help=(
            "return nonzero only when an expected run lacks a terminal record; explicit "
            "invalid/transport/budget failures are valid denominator rows"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = args.manifest or PROJECT_ROOT / "data" / "manifests" / (
        "formal.public.json" if args.frozen_formal else "smoke.public.json"
    )
    token_budgets = args.max_answer_tokens or (
        [512] if args.frozen_smoke else list(ANSWER_TOKEN_BUDGETS)
    )
    call_budgets = args.max_model_calls or (
        [2] if args.frozen_smoke else list(CALL_BUDGETS)
    )
    concurrency = args.concurrency
    if concurrency is None:
        concurrency = 8 if args.frozen_smoke else _env_int("COORDCAP_CONCURRENCY", 4)
    timeout = args.timeout
    if timeout is None:
        timeout = _env_float("COORDCAP_TIMEOUT_SECONDS", 180.0)
    models = _split(args.models)
    if not models:
        models = _split([os.getenv("COORDCAP_MODELS", "")])
    if not models and os.getenv("OPENROUTER_MODEL"):
        models = [str(os.environ["OPENROUTER_MODEL"])]
    if not models:
        raise SystemExit("provide --models or set COORDCAP_MODELS/OPENROUTER_MODEL")
    config = RunnerConfig(
        public_manifest_path=manifest,
        models=models,
        methods=args.methods,
        max_answer_tokens=token_budgets,
        max_model_calls=call_budgets,
        order_variants=_split(args.order_variants),
        enforce_frozen_smoke=args.frozen_smoke,
        include_frozen_reverse_panel=args.frozen_formal,
        raw_root=args.raw_root,
        parsed_root=args.parsed_root,
        cache_root=args.cache_root,
        audit_log=args.audit_log,
        expected_ledger_path=args.expected_ledger,
        overwrite_expected_ledger=args.overwrite_expected_ledger,
        concurrency=concurrency,
        timeout_seconds=timeout,
        temperature=args.temperature,
        api_seed=args.api_seed,
        base_url=args.base_url,
        resume=not args.no_resume,
        use_cache=not args.no_cache,
        repair_invalid=not args.no_repair,
    )
    if args.ledger_only:
        ledger = write_expected_run_ledger(config)
        print(
            json.dumps(
                {
                    "protocol_version": ledger["protocol_version"],
                    "public_manifest_sha256": ledger["public_manifest_sha256"],
                    "execution_config_sha256": ledger["execution_config_sha256"],
                    "expected_runs": len(ledger["runs"]),
                    "expected_ledger_path": str(config.expected_ledger_path),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    summary = asyncio.run(run_coordcap(config))
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if args.strict_exit:
        if summary["terminal_records"] != summary["expected_runs"]:
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
