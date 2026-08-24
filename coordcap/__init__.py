"""CoordCap deterministic coordination-capacity protocol."""

from .canonical import canonical_bytes, canonical_json, canonical_sha256, derive_seed
from .oracle import compute_oracle, solve_public_task
from .tasks import (
    CONFLICT_LEVELS,
    FORMAL_PRINCIPAL_COUNTS,
    PROTOCOL_VERSION,
    SMOKE_PRINCIPAL_COUNTS,
    TASK_FAMILIES,
    generate_manifest_pair,
)
from .validation import independent_solve_public_task, validate_manifest_pair

__all__ = [
    "CONFLICT_LEVELS",
    "FORMAL_PRINCIPAL_COUNTS",
    "PROTOCOL_VERSION",
    "SMOKE_PRINCIPAL_COUNTS",
    "TASK_FAMILIES",
    "canonical_bytes",
    "canonical_json",
    "canonical_sha256",
    "compute_oracle",
    "derive_seed",
    "generate_manifest_pair",
    "independent_solve_public_task",
    "solve_public_task",
    "validate_manifest_pair",
]

__version__ = PROTOCOL_VERSION
