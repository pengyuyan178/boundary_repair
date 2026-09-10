# BoundaryRepair 0.2.0 — 有限语义实现版

本版本在 0.1.0 架构基础上实现了原有算法/适配器入口，并加入可执行测试和独立离线 smoke。
**这不是全语言形式验证器，也不是已经取得 SWE-bench 成绩的系统。**

## 当前完成到哪里

| 层 | 可执行能力 | 明确边界 |
|---|---|---|
| 数据与存储 | JSON/JSONL/映射投影、字段白名单、逐题轨迹、哈希绑定、官方预测三字段 | 不是恶意 Python 插件的进程级数据沙箱 |
| 逻辑内核 | 显式有限域、类型检查、完整枚举、MUST/MAY 蕴含、证书摘要、最少特征选择 | 上限内的有限理论；不是无限整数/任意 JS 求解 |
| 源码前端 | TypeScript Compiler API 解析 JS/JSX/TS/TSX，UTF-8 精确范围，源码哈希 | 不运行目标代码；CSS/R 等走未证明的文本范围 |
| 模块一 | 来源校验、实体候选、有限解释、MUST/MAY/FRAME 分离 | 模型抽取及视觉对应仍可能错误；MUST 只相对于声明理论 |
| 模块二 | 纯布尔函数入口的接口可表达性、四前提检查、最小区分特征、UNKNOWN 保留 | 不证明 UI 到函数入口的应用级可达性；复杂别名/渲染返回 UNKNOWN |
| 模块三 | 作用域计划、词典序风险、布尔语法合成、单次普通空洞生成、哈希/语法/diff 校验 | 消费通路和别名等通用修改尚无完整语义证明；不清空未解决义务 |
| 模型 | 单次 Chat Completions HTTP、显式 fixture、输出用量记账、原 issue 图像传输 | 真实供应商未实测；JSON mode 后仍做本地 schema 校验 |
| 工作区 | 真实 Git archive 导出；受限 Docker archive 适配器 | Docker 在交付环境仅模拟接口验证，未连接守护进程 |
| 评分 | 独立官方 harness 调用、版本/镜像检查、逐题 report.json 解析 | 没有真实 benchmark 跑分；fixture 批次禁止正式评分 |

源码中的 `UNKNOWN`、`PARTIAL` 是算法的有效输出，不等于假装完成全语言分析。
原始实现入口不再通过 `ImplementationRequired` 留空；Protocol 的 `...` 仍是接口声明。

## 放置方式

将 ZIP 内的 `boundary_repair/` 放入：

```text
E:\论文\newGUIRepair\code\boundary_repair\
```

不会覆盖 `code/.env`、`code/configs`、`baseline/` 或它们的 `.git`。
升级前保留旧方法目录及你自己修改过的配置；本包是完整目录，不是针对用户未提供改动的合并补丁。
本次没有读取真实 Windows/SSH 文件、API 密钥、数据集或 Agents.md 内容。

## 本地启动

Python 运行部分仅使用标准库；语法前端另需 Node.js 和固定的 TypeScript 5.8.3。

```powershell
Set-Location 'E:\论文\newGUIRepair\code\boundary_repair'
npm.cmd ci --ignore-scripts --no-audit --no-fund
& 'D:\miniconda\python.exe' run.py doctor --config configs/local.json
& 'D:\miniconda\python.exe' -m unittest discover -s tests -v
& 'D:\miniconda\python.exe' scripts/smoke.py --output '.\smoke-output-001'
```

若已经有可信的 TypeScript 5.8.3 包目录，可通过 smoke 的 `--parser-module` 或
配置 `integration.parser_module` 直接指定。不要指向 benchmark 目标仓库的依赖目录。
交付环境使用的是已安装的 5.8.3，在线 `npm ci` 因网络条件未验证。
`--output` 必须是不存在的新目录，避免覆盖结果。Git 和 Node 必须在 PATH。

**smoke 使用明确标注的证据 fixture，不访问 API、不读取你的数据、不启动 Docker。**
fixture 内只有原始证据的结构化抽取，没有最终 patch。修复表达式由有限合成器计算。
独立输出校验不回传给生成器，也不计入官方 benchmark。

## 真正生成任务前必须配置的内容

读取 `docs/EXPERIMENTS.md`。匹配已有 `.env` 的三个变量名（不是把密钥写进 JSON），
设置真实 benchmark 目标镜像清单、可信 Node/TypeScript 路径；正式评分另需
`harness_python` 的实际可执行文件和明确的 `harness_revision`。
`MODEL_BASE_URL` 可为 HTTPS API 根路径 `/v1` 或完整 `/chat/completions` 路径。
不支持当前 HTTP schema/seed/JSON mode 的供应商会明确报错，不自动改协议重试。

```powershell
& 'D:\miniconda\python.exe' run.py inspect --config configs/local.json --repo 'chartjs/Chart.js'
& 'D:\miniconda\python.exe' run.py generate --config configs/local.json --repo 'chartjs/Chart.js' --limit 1 --batch 'chartjs-dev-001'
```

`doctor` 仅做只读存在性检查，不证明 API/Docker 可用；未配置镜像或 API 时 generate 会失败并写明状态。

## 项目调用方向

```text
CLI → config / dataset → bootstrap → runner
  → workspace.open_base
  → SpecificationRecovery (S1—S4)
  → ExpressivityLocalization (L1—L4)
  → ScopeSynthesis (G1—G3) → ProgramAdapter.materialize
  → predictions.jsonl / trajectory

独立 evaluate 命令 → OfficialDockerEvaluator → 官方报告
```

评测对象不被注入生成流水线。三个模块各自有普通对照，实现八种独立消融接线。
共享的是数据/候选/输出接口，不是研究模块的求解结果。

## 自检与证据

详细运行结果见 `VERIFICATION.json`、`verification/tests.log`、`verification/coverage.txt`。
`verification/function_audit.json` 按函数列出实际语句覆盖与测试范围；`docs/API_REFERENCE.md`
从真实源码生成函数签名和注释。覆盖率不等于正确率，mock 通过不等于真实服务通过。

## 实验产物

默认 `result/method/boundary_repair/<batch>/`，不会写入 baseline 结果目录。
保存 summary、predictions、results，以及每题 patch/input_context/trajectory/logs/result_data。
`generated` 与 `resolved` 分开：生成侧永远不将成功写 diff 当作修复成功。

## 本版本仍需后续验证/扩展

没有真实模型调用、公共图像端到端感知验证、真实 Docker 或真实任务修复率。
没有实现消费通路/渲染分组/缓存身份的完整程序语义、跨函数别名证明、生成副本构建同步。
没有多候选动态评分/反馈循环；多次采样配置表示允许上限，当前执行仍只填充一个选定计划。
不能据此宣称每个真实任务都能修复或当前方法优于 baseline。
