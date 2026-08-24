#!/usr/bin/env python3
"""Generate CoordCap PDF figures from evaluator output."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from coordcap.assets import write_figures  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", type=Path, default=ROOT / "results" / "evaluation_results.json")
    parser.add_argument("--results-root", type=Path, default=ROOT / "results")
    args = parser.parse_args(argv)
    bundle = json.loads(args.evaluation.read_text(encoding="utf-8"))
    paths = write_figures(bundle, args.results_root)
    print(json.dumps({key: str(path) for key, path in paths.items()}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
