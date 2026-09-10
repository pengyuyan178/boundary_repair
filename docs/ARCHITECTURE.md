# 实现架构与依赖边界

版本：0.2.0。`domain` 描述不可变输入/输出；`kernel` 为有限、确定的算法；
`ports` 声明外部能力；`adapters` 承担 I/O；`algorithms` 连接研究步骤；
`bootstrap` 唯一选择真实/fixture/对照实现；`experiments` 管理批次与独立评分。

## 数据流

`TaskInput → RepositorySnapshot → EvidenceBundle → EntityBinding[] → InterpretationSpace → ContractSet
→ RepairBoundary[] → LocalRepairModel → BoundaryAssessment[] → PatchPlan[] → HoleFilling[] → PatchArtifact`。

所有后续步骤只拿显式对象，不拿原始 dataset record。生成器不持有 Evaluator。
`contracts.must + contracts.frames` 才是硬义务；`contracts.may` 放入 `PatchPlan.soft_obligations`，
不能被有限合成器或 materialize 意外提升为强制条件。

## 接口兼容

原来 `adapters/integrations.py` 中的类名保留为重新导出，具体实现拆到 model/logic/program/workspace。
原有 leaf 方法签名基本保留；新增配置、精确字节范围、模型元数据、soft_obligations 使用默认字段兼容。
不引入循环 Agent、失败数据库或动态任务队列。

## 可信计算边界

模型只能提供带来源的证据或语法空洞，不允许给出可达性/完备性/UNSAT 证明标志。
这些标志由有限求解器和支持的源码摘要产生。SOURCE 与 generated 文件均需 hash 校验。
TypeScript 前端是可信工具代码；目标仓库源码作为字符串输入，不 import/require 目标项目。

生产文件通过 exact-commit archive 提取，不 checkout 用户工作树。不全局更改 safe.directory。
本地 Git trust_directory 是操作者显式授权的单次命令配置。Docker 使用固定 digest、无网络、只读、
不挂载用户项目或密钥；真正 Docker 行为尚需服务器验证。

## 不支持不等于不可表达

无法建立局部模型 → UNKNOWN；有限域超过上限 → UNKNOWN；缺失 witness/语义覆盖 → UNKNOWN。
UNKNOWN 可以进入一次普通代码填充，但结果保留未证明义务。不能把证明覆盖率与生成覆盖率混合。

## 预算

每题单个 BudgetLedger。模型调用与生成 token 全局计账；候选计划和思路预先计数；
所有步骤检查期限，子进程有超时和进程组清理。DNS 和慢速传输不属于严格对抗式 OS 资源证明；
临时输出大小限制不等于文件系统配额。正式实验应另有服务器资源管理。
