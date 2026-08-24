"""Canonical serialization and seed derivation for CoordCap.

The protocol hashes the UTF-8 representation of compact, sorted JSON with a
single trailing newline.  Seed derivation uses SHA-256 rather than Python's
process-randomized ``hash`` function, so ``PYTHONHASHSEED`` cannot affect task
generation.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, TypeVar


T = TypeVar("T")


def canonical_json(value: Any) -> str:
    """Return compact canonical JSON without a trailing newline."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    """Return the exact byte representation used for files and hashes."""

    return (canonical_json(value) + "\n").encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def derive_seed(master_seed: int, *parts: object) -> int:
    """Derive a stable unsigned 64-bit seed from typed components."""

    if isinstance(master_seed, bool) or not isinstance(master_seed, int):
        raise TypeError("master_seed must be an integer")
    payload = canonical_json(
        {"master_seed": master_seed, "parts": [str(part) for part in parts]}
    ).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def shuffled(values: Iterable[T], seed: int) -> list[T]:
    """Return a deterministic Fisher--Yates shuffle without global RNG state."""

    result = list(values)
    # Implement the small PRNG stream directly from SHA-256.  This avoids
    # depending on changes to ``random.Random`` across Python implementations.
    for index in range(len(result) - 1, 0, -1):
        block = hashlib.sha256(f"{seed}:{index}".encode("ascii")).digest()
        selected = int.from_bytes(block[:8], "big") % (index + 1)
        result[index], result[selected] = result[selected], result[index]
    return result


def write_canonical_json(path: str | Path, value: Any) -> None:
    """Atomically write canonical JSON.

    Experiment entry points use this helper; generators themselves remain pure.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")


def load_strict_json(path: str | Path) -> Any:
    return json.loads(
        Path(path).read_text(encoding="utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )
