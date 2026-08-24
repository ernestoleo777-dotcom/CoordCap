"""Ensure the isolated CoordCap package wins over the repository namespace.

The parent repository also has a directory named ``coordcap``.  Tests are
invoked from the repository root, so insert the isolated project root before
pytest imports any test module.
"""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
project_root = str(PROJECT_ROOT)
if project_root in sys.path:
    sys.path.remove(project_root)
sys.path.insert(0, project_root)
