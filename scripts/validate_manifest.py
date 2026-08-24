#!/usr/bin/env python3
"""Independently validate a CoordCap public/oracle manifest pair."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from coordcap.canonical import load_strict_json, write_canonical_json  # noqa: E402
from coordcap.validation import validate_manifest_pair  # noqa: E402


LOCKED_PACKAGES = ("aiohttp", "jsonschema", "matplotlib", "numpy", "pytest")
FREEZE_CODE_PATHS = (
    "coordcap/canonical.py",
    "coordcap/schema.py",
    "coordcap/prompts.py",
    "coordcap/tasks.py",
    "coordcap/oracle.py",
    "coordcap/validation.py",
    "coordcap/runner.py",
    "coordcap/evaluation.py",
    "coordcap/statistics.py",
    "coordcap/assets.py",
    "scripts/generate_coordcap.py",
    "scripts/validate_manifest.py",
    "scripts/run_models.py",
    "scripts/evaluate.py",
    "scripts/bootstrap.py",
    "scripts/make_figures.py",
    "scripts/build_paper_assets.py",
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def freeze_audit(public_path: Path, oracle_path: Path) -> dict[str, object]:
    """Capture exact local execution provenance without reading credentials."""

    package_versions: dict[str, str | None] = {}
    for package in LOCKED_PACKAGES:
        try:
            package_versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            package_versions[package] = None
    code_hashes: dict[str, str] = {}
    missing_code_paths: list[str] = []
    for relative in FREEZE_CODE_PATHS:
        source = PROJECT_ROOT / relative
        if source.is_file():
            code_hashes[relative] = _file_sha256(source)
        else:
            missing_code_paths.append(relative)
    protocol_freeze = PROJECT_ROOT / "protocol_freeze.json"
    requirements_lock = PROJECT_ROOT / "requirements.lock.txt"
    status = _git(["status", "--porcelain", "--untracked-files=all"])
    dirty_paths = []
    if status:
        # Preserve Git's quoting for unusual paths and disclose paths/statuses
        # only.  No file content or environment value enters the audit.
        dirty_paths = sorted(line for line in status.splitlines() if line)
    return {
        "audit_schema_version": "coordcap-freeze-audit-1.0.0",
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        },
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
        },
        "package_versions": package_versions,
        "input_file_sha256": {
            "public_manifest": _file_sha256(public_path),
            "oracle_manifest": _file_sha256(oracle_path),
        },
        "freeze_file_sha256": {
            "protocol_freeze.json": _file_sha256(protocol_freeze)
            if protocol_freeze.is_file()
            else None,
            "requirements.lock.txt": _file_sha256(requirements_lock)
            if requirements_lock.is_file()
            else None,
        },
        "code_sha256": code_hashes,
        "missing_code_paths": missing_code_paths,
        "git": {
            "commit": _git(["rev-parse", "HEAD"]),
            "branch": _git(["branch", "--show-current"]),
            "dirty": bool(dirty_paths),
            "dirty_paths": dirty_paths,
        },
        "credentials": {
            "environment_values_read": False,
            "credential_values_recorded": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run schema, hash, leakage, conflict, and dual-solver validation."
    )
    parser.add_argument("--public", type=Path, required=True)
    parser.add_argument("--oracle", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--freeze-audit",
        action="store_true",
        help="Add platform, package, Git, manifest, freeze, and source-code hashes.",
    )
    args = parser.parse_args(argv)
    public = load_strict_json(args.public)
    oracle = load_strict_json(args.oracle)
    report = validate_manifest_pair(public, oracle)
    report.update({"public_path": str(args.public), "oracle_path": str(args.oracle)})
    if args.freeze_audit:
        report["freeze_audit"] = freeze_audit(args.public, args.oracle)
    if args.report:
        write_canonical_json(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
