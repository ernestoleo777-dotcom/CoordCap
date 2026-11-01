"""Offline-first CLI; no model runner is dispatched here."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="python -m coordcap",
        description="Offline tools: demo (educational example), validate (manifest pair), "
                    "evaluate (earlier-protocol saved results). No provider calls.",
        epilog="Use COMMAND --help for command options. Run from the repository checkout.",
    )
    parser.add_argument("command", choices=("demo", "validate", "evaluate"))
    # Let each command own its options, including --help, without rewriting them.
    namespace = parser.parse_args(args[:1])
    remaining = args[1:]
    if namespace.command == "demo":
        from .demo import example_task, format_demo, run_demo
        demo_parser = argparse.ArgumentParser(prog="python -m coordcap demo")
        demo_parser.add_argument("--json", action="store_true", help="Print structured demo output")
        demo_parser.add_argument("--plan", choices=[p["plan_id"] for p in example_task()["plans"]])
        options = demo_parser.parse_args(remaining)
        result = run_demo(options.plan)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
              if options.json else format_demo(result))
        return 0
    script = {"validate": "validate_manifest.py", "evaluate": "evaluate.py"}[namespace.command]
    path = Path(__file__).resolve().parents[1] / "scripts" / script
    if not path.is_file():
        parser.error("This command requires the repository scripts directory.")
    return subprocess.run([sys.executable, str(path), *remaining], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
