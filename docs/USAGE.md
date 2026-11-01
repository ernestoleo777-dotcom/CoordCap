# Using the public code

[English overview](../README.md) · [中文说明](../README.zh-CN.md)

## Environment and local checks / 环境与本地检查

Run from the repository root. Use a Python interpreter compatible with the pinned dependencies in `requirements.lock.txt`; this repository does not declare a tested Python-version matrix.

在仓库根目录执行。依赖版本见 `requirements.lock.txt`；仓库尚未声明经过验证的 Python 版本兼容矩阵。

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock.txt
python -m pytest -q tests
```

On Windows, activate with `.venv\Scripts\activate` instead. Dependency installation accesses package infrastructure. The lightweight tests use local inputs and mocked transport; they do not call model providers or reproduce paper results.

Windows 环境使用 `.venv\Scripts\activate`。安装依赖需要访问软件包服务；轻量测试使用本地输入与模拟通信，不调用模型服务，不复现论文结果。

## Protocol map / 协议区别

| Implementation | Role |
|---|---|
| `tasks.py`, `oracle.py`, `validation.py` | Shared deterministic task generation, scoring reference and independent checks / 共用任务、参考求解与校验 |
| `protocol_freeze.json`, `runner.py`, `evaluation.py`, `scripts/run_models.py`, `scripts/evaluate.py` | Earlier full-output protocol and its CLI / 较早的完整输出协议及命令行入口 |
| `compact_protocol.py`, `compact_runner.py`, `compact_evaluation.py` | Later compact decision protocol, including formal-main configuration checks / 后续精简决策协议，含正式主研究配置检查 |
| `verification_ablation.py` | Budget-verification contrast implementation / 预算验证对照实现 |

The historical `protocol_freeze.json` describes an earlier matrix with 288 base instances. It is not the final paper's 80-task, 640-attempt matrix. The paper's 24-task budget panel has 144 records including 48 reused B0 records. Do not pool these as independent observations or substitute an earlier generated manifest for the final experiment inputs.

历史 `protocol_freeze.json` 记录的是包含 288 个基础实例的早期矩阵，并非论文的 80 任务、640 次尝试。论文的 24 任务预算面板包含 144 条记录，其中 48 条复用 B0；不能全部作为新增独立观察，也不能用早期生成清单替代最终实验输入。

## Inspect command-line interfaces / 查看命令行接口

These help commands do not run model experiments:

以下帮助命令不会运行模型实验：

```bash
python scripts/generate_coordcap.py --help
python scripts/validate_manifest.py --help
python scripts/run_models.py --help
python scripts/evaluate.py --help
```

For a local task-generation demonstration, the generator accepts `--split smoke --output-dir <directory>`. It writes separate public and oracle JSON files and validates them by default. Keep oracle/gold files out of model inputs. This is generated demonstration material, not recovery of the accepted study's frozen inputs or outputs.

若需演示本地任务生成，生成器接受 `--split smoke --output-dir <目录>`，默认生成并校验分离的 public 与 oracle JSON 文件。oracle/gold 文件不能进入模型输入。这是新生成的演示材料，不是恢复原研究的冻结输入或结果。

## Evaluation inputs and outputs / 评价输入与输出

The earlier `scripts/evaluate.py` CLI requires a matching public manifest, oracle manifest, expected-run ledger and parsed terminal outputs. Explicit paths can be supplied with `--public`, `--oracle`, `--expected` and `--parsed-root`. `--results-root` selects the destination; `--require-complete` returns a nonzero exit code for an incomplete run matrix. See `--help` for current arguments.

早期评价入口需要相互匹配的 public 清单、oracle 清单、预期运行账本及解析后的终态输出。可用上述参数指定输入，以 `--results-root` 指定输出；运行矩阵不完整时，`--require-complete` 会返回非零退出码。

The public repository excludes historical provider responses, generated results and private research assets. Missing files must not be replaced by invented records. Final-paper compact results require the matching compact protocol and authentic inputs; the earlier evaluator's completeness flags do not certify the accepted paper's reproduction or scientific validity.

仓库未公开历史 provider 响应、生成结果及私人研究资产。不能用虚构记录补齐输入。最终论文精简协议的评价需要对应协议和真实输入；早期评价器的完整性标记不认证最终论文复现或科学有效性。

## Provider-backed execution / 模型运行

Inspect source and CLI configuration before executing model runners. They can make billable provider requests and require credentials and external assets. No provider-backed run is part of this documentation walkthrough. A complete one-command reproduction of the accepted paper is not supplied.

执行模型运行器前须检查源码与配置，它们可能发起收费请求，需要密钥和外部材料。本指南没有执行模型实验，也未提供最终论文的一键完整复现流程。
