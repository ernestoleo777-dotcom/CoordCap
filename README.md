# CoordCap

**English** | [中文说明](README.zh-CN.md)

Public code projection for:

**More Calls, Not Necessarily Better Coordination: A Budgeted Study of Multi-Principal Reasoning**

Accepted at the **COLM 2026 Workshop on Efficient Reasoning**.

- Paper: [OpenReview final forum](https://openreview.net/forum?id=xNdtZjgf7T)
- Submission: #95
- Publication status: `ACCEPTED_FROZEN`

## Paper

**More Calls, Not Necessarily Better Coordination: A Budgeted Study of Multi-Principal Reasoning**

Hongchen Li<br>
Institute for Advanced Study, Shenzhen University

COLM 2026 — The 2nd Workshop on Efficient Reasoning

[OpenReview](https://openreview.net/forum?id=xNdtZjgf7T)

The final camera-ready paper is externally bound and frozen.

## Scientific summary

CoordCap studies finite-catalogue multi-principal decision making using deterministic local solver scoring. In the evaluated two-route, blind, model-generated, tool-free verification protocol, additional verification calls did not yield reliable coordination gains; a two-stage audit-and-revision condition increased cost and worsened pooled hard-constraint violations and regret.

This result is specific to the evaluated protocol and does not imply that tool-grounded, externally verified, or general test-time reasoning is ineffective.

## Status

```text
Publication: ACCEPTED_FROZEN
Scientific work: COMPLETE
Public projection: MAINTENANCE_ONLY
```

## Included scope

This rights-scoped projection contains the author-owned Python implementation, the frozen public protocol configuration, canonical orchestration scripts, dependency metadata, and lightweight CPU-only tests needed to inspect the method and its repository structure.

It intentionally excludes datasets, provider responses, API logs and caches, model weights, generated experiment results, reviewer or submission-system material, camera-ready or supplementary PDFs, conference templates, internal audits, and unrelated repository content. No experimental number or scientific claim was rewritten for this projection.

## License

The author-owned source code and original repository documentation in this projection are licensed under the [Apache License 2.0](LICENSE) (`Apache-2.0`). See [LICENSE_STATUS.md](LICENSE_STATUS.md) for the precise scope. This grant does not cover datasets, model weights or checkpoints, generated outputs, provider responses, papers or supplementary manuscripts, conference templates, external dependencies, or third-party assets. Excluded material receives no license through this repository.

## Repository guide

| Path | Contents |
|---|---|
| `coordcap/tasks.py`, `oracle.py`, `validation.py` | Deterministic tasks, reference solver and independent checks |
| `coordcap/compact_protocol.py`, `compact_runner.py`, `compact_evaluation.py` | Compact decision protocol, formal-main execution checks and evaluation |
| `coordcap/verification_ablation.py` | Budget-verification contrast implementation |
| `protocol_freeze.json`, `coordcap/runner.py`, `coordcap/evaluation.py` | Earlier protocol configuration and implementation |
| `scripts/`, `tests/` | Command-line tools and lightweight local checks |
| [Usage guide](docs/USAGE.md) | Setup, protocol distinctions and evaluation input requirements (English / 中文) |
| [Citation](CITATION.cff), [changelog](CHANGELOG.md) | Paper citation and public maintenance history |

The accepted main study contains 80 tasks and 640 scored attempts. The 24-task budget panel has 144 records, including 48 reused B0 records; these are not all additional independent observations. The historical `protocol_freeze.json` describes an earlier 288-instance matrix, not the final paper's compact study. The public repository does not include the historical result records. See the [protocol map](docs/USAGE.md#protocol-map--协议区别) before using the runners or evaluators.

## Try the offline demo

After installing the dependencies, run:

```bash
python -m coordcap demo
```

A newly authored educational task compares four meeting plans for two teams. The output shows each plan's hard-constraint violations, utilities and regret, checks two separate solver implementations, and selects a deterministic reference plan. It needs no API key and makes no provider requests. It is not a paper task, a model prediction, or a reproduction of the accepted results.

Choose an over-budget plan to inspect a failure, or request structured output:

```bash
python -m coordcap demo --plan morning_over_budget
python -m coordcap demo --json
python -m coordcap --help
```

The unified offline entry point also exposes `validate` and `evaluate`; the latter wraps the earlier-protocol evaluator, not the final compact study. See the [usage guide](docs/USAGE.md). GitHub runs the existing tests and demo in a separate functionality workflow; passing checks do not validate paper results.

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
