# CoordCap

CoordCap is the implementation accompanying the accepted, non-anonymous COLM 2026 project **“More Calls, Not Necessarily Better Coordination: A Budgeted Study of Multi-Principal Reasoning.”** Acceptance status is supplied by the repository owner; this is the rights-scoped public projection.

## Included scope

This rights-scoped projection contains the author-owned Python implementation, the frozen public protocol configuration, canonical orchestration scripts, dependency metadata, and lightweight CPU-only tests needed to inspect the method and its repository structure.

It intentionally excludes datasets, provider responses, API logs and caches, model weights, generated experiment results, reviewer or submission-system material, camera-ready or supplementary PDFs, conference templates, internal audits, and unrelated repository content. No experimental number or scientific claim was rewritten for this projection.

## License

The author-owned source code and original repository documentation in this projection are licensed under the [Apache License 2.0](LICENSE) (`Apache-2.0`). See [LICENSE_STATUS.md](LICENSE_STATUS.md) for the precise scope. This grant does not cover datasets, model weights or checkpoints, generated outputs, provider responses, papers or supplementary manuscripts, conference templates, external dependencies, or third-party assets. Excluded material receives no license through this repository.

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

## Mechanical repository integrity

The public workflow consumes the released ResearchAuditKit `v0.1.0-rc.3` composite Action through immutable commit `72ee132038a36d8678da11e86d3b953726a5e9a7` and runs `rak audit` with the repository policy. The Action owns its Python and PyYAML bootstrap, writes temporary results only to runner-temporary storage, does not upload repository contents, and neither installs CoordCap dependencies nor executes CoordCap code. Bootstrap requires GitHub Action and Python package infrastructure; the repository analysis itself runs locally on the runner without a hosted ResearchAuditKit service.

ResearchAuditKit checks declared repository files and structurally observable release blockers mechanically. CoordCap and ResearchAuditKit share the same owner, so this is a self-owned public consumer integration, not independent external adoption. Passing the preflight does not validate CoordCap's scientific claims, experimental conclusions, novelty, acceptance status, correctness, or reproducibility. ResearchAuditKit remains a prerelease and is not installed from PyPI.

## Repository provenance

- [Projection manifest](PUBLIC_PROJECTION_MANIFEST.json)
- [Asset provenance](ASSET_PROVENANCE.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)
- [Project status](PROJECT_STATUS.md)

## Limitations

This public projection has not been independently adopted and does not contain the private or redistribution-sensitive assets required to rerun provider-backed experiments. Passing repository checks does not validate scientific claims, experimental correctness, novelty, publication merit, or general reproducibility.
