#!/usr/bin/env python3
"""Build all tables, figures, macros, fact sheets, and failure assets."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from coordcap.assets import build_paper_assets  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", type=Path, default=ROOT / "results" / "evaluation_results.json")
    parser.add_argument("--results-root", type=Path, default=ROOT / "results")
    parser.add_argument("--paper-root", type=Path, default=ROOT / "paper")
    args = parser.parse_args(argv)
    bundle = json.loads(args.evaluation.read_text(encoding="utf-8"))
    paths = build_paper_assets(bundle, args.results_root, paper_root=args.paper_root)
    print(json.dumps({key: str(path) for key, path in paths.items()}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
