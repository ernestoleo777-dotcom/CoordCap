#!/usr/bin/env python3
"""Generate canonical, strictly separated CoordCap public/oracle manifests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from coordcap.canonical import canonical_sha256, write_canonical_json  # noqa: E402
from coordcap.tasks import (  # noqa: E402
    DEFAULT_MASTER_SEED,
    generate_manifest_pair,
    instance_specs,
)
from coordcap.validation import validate_manifest_pair  # noqa: E402


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(
        description="Generate byte-deterministic CoordCap manifests without API access."
    )
    argument_parser.add_argument("--split", choices=("smoke", "formal"), required=True)
    argument_parser.add_argument("--seed", type=int, default=DEFAULT_MASTER_SEED)
    argument_parser.add_argument(
        "--output-dir", type=Path, default=PROJECT_ROOT / "data" / "manifests"
    )
    argument_parser.add_argument("--public-output", type=Path)
    argument_parser.add_argument("--oracle-output", type=Path)
    argument_parser.add_argument("--shard-index", type=int)
    argument_parser.add_argument("--shard-count", type=int)
    argument_parser.add_argument(
        "--generation-order", choices=("canonical", "reverse"), default="canonical"
    )
    argument_parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Write after generation without the independent-solver audit (not for frozen runs).",
    )
    return argument_parser


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    order = list(range(len(instance_specs(args.split))))
    if args.generation_order == "reverse":
        order.reverse()
    public, oracle = generate_manifest_pair(
        args.split,
        args.seed,
        generation_order=order,
        shard_index=args.shard_index,
        shard_count=args.shard_count,
    )
    if not args.skip_validation:
        validation = validate_manifest_pair(public, oracle)
    else:
        validation = {"status": "skipped"}
    if args.shard_count is None:
        stem = args.split
    else:
        stem = f"{args.split}.shard_{args.shard_index:02d}-of-{args.shard_count:02d}"
    public_path = args.public_output or args.output_dir / f"{stem}.public.json"
    oracle_path = args.oracle_output or args.output_dir / f"{stem}.oracle.json"
    if public_path.resolve() == oracle_path.resolve():
        raise ValueError("public and oracle outputs must be different files")
    write_canonical_json(public_path, public)
    write_canonical_json(oracle_path, oracle)
    print(
        json.dumps(
            {
                "status": "ok",
                "split": args.split,
                "instance_count": public["instance_count"],
                "expected_full_instance_count": len(instance_specs(args.split)),
                "public_path": str(public_path),
                "oracle_path": str(oracle_path),
                "public_sha256": canonical_sha256(public),
                "oracle_sha256": canonical_sha256(oracle),
                "validation": validation,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
