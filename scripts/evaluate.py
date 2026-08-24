#!/usr/bin/env python3
"""Evaluate CoordCap terminals against the frozen expected-run ledger."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from coordcap.evaluation import evaluate_files, write_evaluation  # noqa: E402
from coordcap.statistics import BOOTSTRAP_SAMPLES, BOOTSTRAP_SEED  # noqa: E402


def _default_manifest(split: str, kind: str) -> Path:
    if kind == "expected_runs":
        candidates = (
            ROOT / "outputs" / f"{split}.expected_runs.json",
            ROOT / "outputs" / split / "expected_runs.json",
            ROOT / "outputs" / "expected_runs.json",
        )
        return next((path for path in candidates if path.is_file()), candidates[0])
    candidates = (
        ROOT / "data" / "manifests" / f"{split}.{kind}.json",
        ROOT / "data" / "manifests" / f"{split}_{kind}_manifest.json",
        ROOT / "data" / "manifests" / f"{split}_{kind}.json",
        ROOT / "data" / "manifests" / f"coordcap_{split}_{kind}.json",
        ROOT / "data" / "manifests" / f"{kind}_{split}.json",
    )
    return next((path for path in candidates if path.is_file()), candidates[0])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("smoke", "formal"), default="smoke")
    parser.add_argument("--public", type=Path)
    parser.add_argument("--oracle", type=Path)
    parser.add_argument("--expected", type=Path)
    parser.add_argument("--parsed-root", type=Path)
    parser.add_argument("--results-root", type=Path, default=ROOT / "results")
    parser.add_argument("--bootstrap-samples", type=int, default=BOOTSTRAP_SAMPLES)
    parser.add_argument("--bootstrap-seed", type=int, default=BOOTSTRAP_SEED)
    parser.add_argument("--require-complete", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    public = args.public or _default_manifest(args.split, "public")
    oracle = args.oracle or _default_manifest(args.split, "oracle")
    expected = args.expected or _default_manifest(args.split, "expected_runs")
    split_parsed = ROOT / "outputs" / "parsed" / args.split
    parsed = args.parsed_root or (
        split_parsed if split_parsed.is_dir() else ROOT / "outputs" / "parsed"
    )
    for label, path in (("public", public), ("oracle", oracle), ("expected", expected)):
        if not path.is_file():
            raise SystemExit(f"{label} input does not exist: {path}")
    bundle = evaluate_files(
        public,
        oracle,
        expected,
        parsed,
        bootstrap_samples=args.bootstrap_samples,
        bootstrap_seed=args.bootstrap_seed,
    )
    paths = write_evaluation(bundle, args.results_root)
    audit = bundle["audit"]
    print(
        json.dumps(
            {
                "split": bundle["split"],
                "execution_config_sha256": bundle["execution_config_sha256"],
                "expected_runs": audit["expected_runs"],
                "scored_runs": audit["scored_runs"],
                "missing_runs": audit["missing_runs"],
                "duplicate_runs": audit["duplicate_runs"],
                "matrix_complete": audit["matrix_complete"],
                "publication_ready": audit["publication_ready"],
                "outputs": {key: str(path) for key, path in paths.items()},
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 2 if args.require_complete and not audit["matrix_complete"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
