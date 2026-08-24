# CoordCap

CoordCap is the implementation accompanying the accepted, non-anonymous COLM 2026 project **“More Calls, Not Necessarily Better Coordination: A Budgeted Study of Multi-Principal Reasoning.”** Acceptance status is supplied by the repository owner; this local projection is not yet a public GitHub repository.

## Included scope

This rights-scoped projection contains the author-owned Python implementation, the frozen public protocol configuration, canonical orchestration scripts, dependency metadata, and lightweight CPU-only tests needed to inspect the method and its repository structure.

It intentionally excludes datasets, provider responses, API logs and caches, model weights, generated experiment results, reviewer or submission-system material, camera-ready or supplementary PDFs, conference templates, internal audits, and unrelated repository content. No experimental number or scientific claim was rewritten for this projection.

## License status

Public software licensing has not yet been selected. See [LICENSE_STATUS.md](LICENSE_STATUS.md). Until the owner selects and applies a license, this projection must not be represented as granting public reuse or redistribution rights. Dataset, model, paper, and third-party asset rights remain separate from any future software license.

## Local environment

The recorded Python dependency set is in `requirements.lock.txt`. A local development environment can be prepared with:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock.txt
```

No packaged distribution or one-command scientific reproduction is claimed. The model-running scripts require external providers and assets that are not included here; they were not executed while creating this projection.

## Lightweight checks

The included tests exercise repository-local generation, runner plumbing, and evaluation helpers without downloading datasets or weights:

```bash
python -m pytest -q tests
```

These checks do not reproduce the accepted experimental results.

## Repository provenance

- [Projection manifest](PUBLIC_PROJECTION_MANIFEST.json)
- [Asset provenance](ASSET_PROVENANCE.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)
- [Project status](PROJECT_STATUS.md)

## Limitations

This projection has not been independently adopted, is not yet hosted publicly, and does not contain the private or redistribution-sensitive assets required to rerun provider-backed experiments. Passing repository checks does not validate scientific claims, experimental correctness, novelty, publication merit, or general reproducibility.
