# 原始占位函数逐项自检

以下 23 项与 0.1.0 的待实现入口一一对应。每项已有可执行函数体，且本次测试执行过该函数体。
语句覆盖不等于每个分支正确；适配器模拟测试不等于真实外部服务成功。

| 序号 | 原函数 | 当前文件 | 语句覆盖 | 验证方式 / 限制 |
|---|---|---|---|---|
| 1 | `FrozenModelAdapter.complete` | `src/boundary_repair/adapters/model.py` | 36/44 | 真实 loopback HTTP + 显式 fixture；无真实模型 |
| 2 | `LogicAdapter.check` | `src/boundary_repair/adapters/logic.py` | 28/31 | 有限算法及端到端合成输入实测 |
| 3 | `ProgramAdapter.index` | `src/boundary_repair/adapters/program.py` | 21/21 | 真实 TypeScript/Git/Node；仅支持的局部语义 |
| 4 | `ProgramAdapter.summarize` | `src/boundary_repair/adapters/program.py` | 28/35 | 真实 TypeScript/Git/Node；仅支持的局部语义 |
| 5 | `ProgramAdapter.effects` | `src/boundary_repair/adapters/program.py` | 9/10 | 真实 TypeScript/Git/Node；仅支持的局部语义 |
| 6 | `ProgramAdapter.materialize` | `src/boundary_repair/adapters/program.py` | 72/81 | 真实 TypeScript/Git/Node；仅支持的局部语义 |
| 7 | `DockerWorkspaceAdapter.open_base` | `src/boundary_repair/adapters/workspace.py` | 20/23 | 模拟 Docker 调用 + 真实 archive 解包；无真实 daemon |
| 8 | `PlainControls.recover` | `src/boundary_repair/algorithms/controls.py` | 6/6 | 8 个组合的模型 double；非 benchmark |
| 9 | `PlainControls.locate` | `src/boundary_repair/algorithms/controls.py` | 2/2 | 8 个组合的模型 double；非 benchmark |
| 10 | `PlainControls.synthesize` | `src/boundary_repair/algorithms/controls.py` | 11/12 | 8 个组合的模型 double；非 benchmark |
| 11 | `ExpressivityLocalization.enumerate_boundaries` | `src/boundary_repair/algorithms/expressivity.py` | 1/1 | 有限算法及端到端合成输入实测 |
| 12 | `ExpressivityLocalization.build_local_model` | `src/boundary_repair/algorithms/expressivity.py` | 20/24 | 有限算法及端到端合成输入实测 |
| 13 | `ExpressivityLocalization.assess_expressivity` | `src/boundary_repair/algorithms/expressivity.py` | 13/14 | 有限算法及端到端合成输入实测 |
| 14 | `ExpressivityLocalization.rank_boundaries` | `src/boundary_repair/algorithms/expressivity.py` | 4/4 | 有限算法及端到端合成输入实测 |
| 15 | `SpecificationRecovery.extract_evidence` | `src/boundary_repair/algorithms/specification.py` | 5/5 | 有限算法及端到端合成输入实测 |
| 16 | `SpecificationRecovery.bind_entities` | `src/boundary_repair/algorithms/specification.py` | 10/10 | 有限算法及端到端合成输入实测 |
| 17 | `SpecificationRecovery.build_interpretation_space` | `src/boundary_repair/algorithms/specification.py` | 10/11 | 有限算法及端到端合成输入实测 |
| 18 | `SpecificationRecovery.derive_contracts` | `src/boundary_repair/algorithms/specification.py` | 21/24 | 有限算法及端到端合成输入实测 |
| 19 | `ScopeSynthesis.enumerate_plans` | `src/boundary_repair/algorithms/synthesis.py` | 20/23 | 有限算法及端到端合成输入实测 |
| 20 | `ScopeSynthesis.select_minimal_scope` | `src/boundary_repair/algorithms/synthesis.py` | 17/18 | 有限算法及端到端合成输入实测 |
| 21 | `ScopeSynthesis.fill_holes` | `src/boundary_repair/algorithms/synthesis.py` | 8/8 | 有限算法及端到端合成输入实测 |
| 22 | `build_workspace` | `src/boundary_repair/bootstrap.py` | 2/6 | 工厂选择/拒绝错误模式测试 |
| 23 | `OfficialDockerEvaluator.evaluate` | `src/boundary_repair/experiments/evaluation.py` | 56/76 | 模拟 harness/Docker/报告；无真实评分 |

## 全函数清单

`function_audit.json` 同时包含新增辅助函数、接口声明、缺失语句行号。
没有用 100% 正确率表述覆盖率；未覆盖分支在 coverage.json 中保留。
