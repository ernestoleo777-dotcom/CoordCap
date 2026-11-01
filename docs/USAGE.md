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

## Unified offline commands / 统一离线命令

```bash
python -m coordcap demo
python -m coordcap demo --plan morning_over_budget
python -m coordcap demo --json
python -m coordcap validate --help
python -m coordcap evaluate --help
```

`demo` creates an in-memory educational example, without reading datasets, writing files or contacting providers. Two teams value different meeting times (600 points) and privacy (400 points). Cost must not exceed 3. A public evening plan is feasible but dominated by the private evening plan; an over-budget morning plan violates shared and team constraints. Per-team regret is the nonnegative difference from that team's best feasible utility. Utility and regret are still displayed for infeasible plans, but do not make them acceptable. Reference selection maximizes weighted welfare, minimizes worst regret, then breaks ties using canonical plan ordering; it does not imply a unique fair choice. `--plan` inspects a chosen candidate, and an infeasible candidate is a successful inspection rather than a CLI failure. `--json` returns the full task and evaluations with an explicit educational scope label.

`demo` 在内存中创建教学示例，不读取数据集、不写文件、不访问服务。两队各自的时间偏好占600分，私密性占400分，费用上限为3。公开晚间方案可行但被私密晚间方案支配；超预算上午方案违反共享及各队约束。每队后悔值为其最佳可行效用与当前效用之差，最低为0；不可行方案即使有效用或较低后悔值也不被接纳。参考选择按加权总效用、最差后悔值、规范排序依次决胜，不代表唯一公平选择。`--plan` 用于检查指定候选，检查到不可行方案仍正常退出；`--json` 输出完整任务和评分，并明确标注教学用途。

`validate` forwards arguments and exit codes to `scripts/validate_manifest.py`; `evaluate` does the same for the earlier-protocol `scripts/evaluate.py`. No model-running command is exposed by this CLI. These wrappers require the repository checkout and retain the original inputs and failure behavior. Use explicit input paths when evaluating saved records.

`validate` 与 `evaluate` 分别转交参数给既有清单校验脚本与早期评价脚本，并保留其退出码；需要完整仓库目录。统一入口不提供模型运行命令。评价保存记录时请明确指定输入路径。

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
