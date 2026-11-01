# CoordCap

[English](README.md) | **中文说明**

**More Calls, Not Necessarily Better Coordination: A Budgeted Study of Multi-Principal Reasoning**

Hongchen Li · 深圳大学高等研究院

COLM 2026 第二届 Efficient Reasoning **Workshop** 已接收论文的公开代码仓库。

[论文与 OpenReview](https://openreview.net/forum?id=xNdtZjgf7T) · [使用指南](docs/USAGE.md) · [引用信息](CITATION.cff) · [更新记录](CHANGELOG.md)

## 研究问题与结论

当多个委托方拥有不同的硬约束与偏好时，模型能否在有限候选方案中作出协调决策？CoordCap 使用确定性的本地求解与评分，比较受限预算下的决策方式。

在论文所评估的两条模型路线、盲式、模型生成且验证过程不使用外部工具的协议中，增加验证调用没有带来可靠的协调收益；两阶段审计与修订条件增加了成本，并使汇总硬约束违反与 regret 指标恶化。结论仅适用于所评估的协议，不能推广为工具验证或一般推理无效。

主研究包含 80 个任务、640 次计分尝试；预算面板包含 24 个任务、144 条记录，其中 48 条 B0 记录复用主研究，不能另计独立样本。这里说明论文研究范围，不代表仓库附带这些原始结果或已重新复现。

## 仓库导航

| 路径 | 内容 |
|---|---|
| `coordcap/tasks.py`、`oracle.py`、`validation.py` | 任务生成、确定性参考求解与独立校验 |
| `coordcap/compact_protocol.py`、`compact_runner.py`、`compact_evaluation.py` | 精简决策协议、运行与评价实现 |
| `coordcap/verification_ablation.py` | 预算验证对照实现 |
| `coordcap/runner.py`、`evaluation.py`、`protocol_freeze.json` | 较早协议的运行、评价和冻结配置；不能直接当成最终论文实验配置 |
| `scripts/`、`tests/` | 命令行工具与本地轻量检查 |
| `docs/USAGE.md` | 环境、离线检查、输入文件与协议区别 |

## 运行离线演示

安装依赖后，在仓库根目录执行：

```bash
python -m coordcap demo
```

这个新建教学任务包含两队与四个会议方案，展示各方案的硬约束违反、效用和后悔值，并核对两套求解实现是否一致，给出确定性的参考选择。无需 API 密钥，不调用模型服务。它不是论文任务、模型预测或原研究复现。

也可以主动选择超预算方案查看失败原因，或输出 JSON：

```bash
python -m coordcap demo --plan morning_over_budget
python -m coordcap demo --json
python -m coordcap --help
```

统一离线入口还提供 `validate` 与 `evaluate`；后者封装的是早期协议评价器，不能替代最终论文精简协议评价。详见[使用指南](docs/USAGE.md)。GitHub 会自动执行测试与演示；这些工程检查不验证论文结果。

## 开始使用

在仓库根目录创建环境并安装依赖：

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock.txt
python -m pytest -q tests
```

安装依赖需要网络；安装后的轻量测试不需要 API 密钥、模型权重或数据下载。它们检查本地生成、运行调度及评分辅助逻辑，不能复现论文实验结果。详细步骤见[使用指南](docs/USAGE.md)。

## 公开范围与复现限制

仓库包含作者拥有权利的 Python 实现、公开协议配置、脚本、依赖清单和轻量测试。它不包含数据集、provider 原始响应、API 日志与缓存、模型权重、生成实验结果、投稿和审稿后台材料、论文或补充材料 PDF、会议模板、内部审计记录及无关研究资产。

模型运行需要外部服务、输入材料与预算。现有脚本并不构成一键复现最终论文的完整流程。重新生成任务也不等于恢复最终论文所用的任务清单、运行账本或原始模型响应。

## 引用与许可

引用时请使用 [CITATION.cff](CITATION.cff) 中的论文信息，或 OpenReview 页面。论文属于 **Workshop**，不是 COLM 主会论文。

[Apache-2.0](LICENSE) 适用于作者拥有权利的源码和原创仓库文档。数据集、模型、输出、provider 响应、论文、会议模板、外部依赖及第三方资产不因本仓库而获得该许可，具体见 [LICENSE_STATUS.md](LICENSE_STATUS.md) 和 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 维护状态与来源

论文结果冻结，公开仓库仅作维护。文档更新不代表新增实验或扩大科学结论。

- [项目状态](PROJECT_STATUS.md)
- [资产来源](ASSET_PROVENANCE.md)
- [公开文件清单与哈希](PUBLIC_PROJECTION_MANIFEST.json)

仓库使用固定提交版本的 ResearchAuditKit `v0.1.0-rc.3` Action 做机械完整性检查。它与 CoordCap 属于同一维护者，不构成独立外部验证；检查通过不证明科学结论、正确性或可复现性。完整工具说明见[英文主页](README.md#mechanical-repository-integrity)。
