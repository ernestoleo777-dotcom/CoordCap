#!/usr/bin/env python3
"""Recompute frozen CoordCap bootstrap intervals from scored expected rows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from coordcap.statistics import (  # noqa: E402
    BOOTSTRAP_SAMPLES,
    BOOTSTRAP_SEED,
    capacity_analysis,
    metric_cells,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", type=Path, default=ROOT / "results" / "evaluation_results.json")
    parser.add_argument("--output", type=Path, default=ROOT / "results" / "bootstrap_results.json")
    parser.add_argument("--samples", type=int, default=BOOTSTRAP_SAMPLES)
    parser.add_argument("--seed", type=int, default=BOOTSTRAP_SEED)
    args = parser.parse_args(argv)
    bundle = json.loads(args.evaluation.read_text(encoding="utf-8"))
    rows = [
        row
        for row in bundle.get("scored_runs", [])
        if isinstance(row, dict) and row.get("order_variant") == "canonical"
    ]
    aggregate_fields = (
        "model",
        "method",
        "max_answer_tokens",
        "max_model_calls",
        "principal_count",
        "conflict_level",
    )
    family_fields = aggregate_fields + ("task_family",)
    audit = dict(bundle.get("audit") or {})
    bootstrap_protocol_compliant = bool(
        args.samples == BOOTSTRAP_SAMPLES and args.seed == BOOTSTRAP_SEED
    )
    audit["bootstrap_protocol_compliant"] = bootstrap_protocol_compliant
    if not bootstrap_protocol_compliant:
        audit["publication_ready"] = False
    payload = {
        "schema_version": bundle.get("schema_version"),
        "protocol_version": bundle.get("protocol_version"),
        "execution_config_sha256": bundle.get("execution_config_sha256"),
        "split": bundle.get("split"),
        "bootstrap": {"samples": args.samples, "seed": args.seed},
        "audit": audit,
        "metrics": {
            "headline_cells": metric_cells(
                rows,
                group_fields=("model", "method"),
                samples=args.samples,
                seed=args.seed,
            ),
            "aggregate_cells": metric_cells(
                rows,
                group_fields=aggregate_fields,
                samples=args.samples,
                seed=args.seed,
            ),
            "cells_by_task_family": metric_cells(
                rows,
                group_fields=family_fields,
                samples=args.samples,
                seed=args.seed,
            ),
            "coordination_consistency": (
                bundle.get("metrics", {}).get("coordination_consistency")
                if isinstance(bundle.get("metrics"), dict)
                else None
            ),
        },
        "capacity": capacity_analysis(rows, samples=args.samples, seed=args.seed),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "rows": len(rows)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
