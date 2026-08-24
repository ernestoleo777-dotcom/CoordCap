# Asset provenance

This projection was constructed from an explicit allowlist bound to source commit `e146ece5d0532b72b8bfed4cf403e32069c83bb2`.

## Included assets

- Python implementation, canonical scripts, and lightweight tests: `AUTHOR_OWNED_RIGHTS_CONFIRMED`, licensed under Apache-2.0 where the CoordCap authors hold licensing rights.
- `protocol_freeze.json` and `requirements.lock.txt`: author-controlled project metadata copied byte-for-byte.
- Public projection documentation and metadata: newly generated for this projection and identified as such in `PUBLIC_PROJECTION_MANIFEST.json`.
- External Python packages: `REFERENCE_ONLY_NOT_COPIED`.

Apache-2.0 covers author-owned source code and original repository documentation only. Dataset, model, paper, generated-output, external-dependency, excluded-content, and third-party rights remain separate and are not granted by this repository.

Source files were relocated into a standalone CoordCap layout without byte changes. The manifest records source Git blobs, source and destination SHA-256 values, and transformation status for every copied file.

## Excluded classes

Datasets and copies, weights and checkpoints, provider responses, API caches and request logs, generated experiment results, reviewer or submission records, anonymous-review material, internal audit and governance records, PDFs and conference templates, unrelated repository content, and any third-party asset with unresolved redistribution rights were not copied.

The projection manifest intentionally does not enumerate confidential excluded filenames. Its own hash is not embedded within itself; the enclosing Git commit and final verification report bind the manifest bytes without recursive self-reference.
