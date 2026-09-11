# BoundaryRepair 通用接口设计 v3

日期：2026-09-11。当前在线协议更新为 **evidence.v3 + edits.v4（程序拥有的编辑边界）**；历史 v3c 结果保持原样：五题真实回归为 4/5 非空可应用 patch、4 题独立官方评分、0 resolved。该历史成绩不是新协议的实验结果。

## 1. 目标与当前证据

目标不是给失败案例逐项增加例外，而是让同一套任务、源码、修改和提交接口承载 SWE-bench Multimodal 全集。语义证明适配器只增强分析，不能成为读源码和生成补丁的前置条件。

本地固定数据版本：`SWE-bench/SWE-bench_Multimodal@4e6662d51c48e475f7f346e4fa09a6f8b31fcaa5`。

2026-09-11 使用现有生成侧 `load_tasks` 白名单实际核验全部任务，未读取 gold/test patch 内容：

| 项目 | dev | test |
|---|---:|---:|
| 任务 | 100 | 480 |
| 仓库 | 5 | 11 |
| 原始附件 | 153 | 649 |
| 无图片任务 | 6 | 46 |
| 单题最多附件 | 6 | 6 |
| 最长 issue 字符数 | 6255 | 135361 |
| GIF URL 数量 | 3 | 76 |
| 必需输入字段完整 | 100/100 | 480/480 |

附件扩展名包括 PNG、GIF、JPG、JPEG 和无后缀 URL；实际格式以下载字节检测为准。开发与测试仓库不重合，共 16 个，因此不能用五个 dev 项目的 AST 形态定义整个修复接口。

- dev SHA-256：`bbc645dcf2557348c749f95f0bcbd2d3064409f2e089432d2d1f24f91a07c87b`。
- test SHA-256：`03653423b955194e857012e3273e6aa57c05ddfd51429657b19b073b2aa4c0b0`。

这项检查只证明 **580 条任务的输入投影可用**，不证明所有镜像可用、附件可下载、仓库可完整解析或修复能成功。

## 2. 不改变论文主链

```text
BenchmarkTask + immutable base snapshot
    → ContextBundle（通用证据、源码检索）
    → ContractResult（MUST / MAY / FRAME + 覆盖状态）
    → BoundarySet（可表达 / 有证书的不可表达 / UNKNOWN）
    → GenerationPlan（一次生成前冻结范围和模式）
    → EditTransaction（一次性生成所有关联修改）
    → PatchCompiler（原子应用、生成 diff、记录检查状态）
    → PatchResult（冻结）
    → 独立官方评分
```

保留三个研究模块，通用能力由它们和所有对照共同使用。没有测试反馈、失败补丁回传、模型纠错重试或按 instance_id 分支。

idea 文档第 1083 行已经要求：不支持的任务在生成前进入弱约束的一次性生成路径。本设计落实这一要求；不是生成失败后再调用普通修复器兜底。

## 3. 核心分离：通用可编辑性不依赖语义可证明性

当前 `ProgramPort` 同时承担全仓索引、局部语义和补丁物化，导致后两者被 JS AST 支持程度绑住。v3 分离为以下稳定契约：

```python
TaskAdapter.project(raw_task) -> BenchmarkTask
WorkspacePort.open_base(task, runtime) -> Snapshot
RepositoryPort.catalog(snapshot, cursor, limits) -> FilePage
RepositoryPort.search(snapshot, query, limits) -> SearchResult
RepositoryPort.read_regions(snapshot, requests, limits) -> RegionCatalog
EvidencePort.prepare(task, regions, limits) -> ContextBundle
SemanticPort.analyze(regions, obligations, limits) -> AnalysisResult
SpecificationPort.recover(context) -> ContractResult
LocalizationPort.locate(context, contracts) -> BoundarySet
SynthesisPort.plan(context, contracts, boundaries) -> GenerationPlan
SynthesisPort.generate(context, contracts, plan) -> EditTransaction
PatchPort.compile(snapshot, plan, transaction) -> PatchResult
```

以上保留目标契约，用于说明能力边界；**不是当前可直接调用的 API 签名**。本次实现使用 `RepositoryPort.source_scope(snapshot, context, query)`，由 `SourceRepository.retrieve` 一次返回有界 `EditScope`；`ProgramPort` 组合通用 Repository/Patch 能力与原有语义接口，没有额外引入 `SemanticPort` 类或可交互目录分页 API。`PatchPort.freeze_plan/compile` 分别冻结计划和编译事务。通用检索与编译不要求 `BooleanReturn`、`Consumer`、`Argument` 或任何特定 AST 节点存在。

### 3.1 BenchmarkTask：只描述任务，不预设修复类型

沿用 `instance_id / repo / base_commit / problem_statement / original_assets` 的生成侧白名单。Docker 运行配置由独立 runtime 提供，不把原始含答案数据行交给算法。

- 原 issue 保留全文与哈希，不强制模型一次读完。
- 附件注册表保留全部原附件。静态图和 GIF 由媒体适配器处理，GIF 的帧位置和采样覆盖可追溯；未读取的帧不能算已经观察。
- 图片不存在、下载失败、格式不支持时记录附件状态；不是删除题目，也不是伪造视觉结论。
- 无图片任务按文本输入运行。
- 长文本分块、检索、分页，并记录未进入模型上下文的范围。上下文覆盖为 PARTIAL 不意味着输入无效。
- 不增加 gold/test patch、修复后参考图、官方测试输出或历史失败轨迹的访问权。

### 3.2 ContextBundle：所有来源 ID 都由程序分配

```text
ContextBundle
  task_ref
  evidence_catalog: EvidenceID → 原文范围或原附件
  region_catalog: RegionID → path / base hash / byte range / 原内容
  read_coverage
  media_status
  diagnostics
```

模型只引用目录中已存在的 `evidence_id`，不再创建第二套 `source_id`。

文本证据的 ID 唯一绑定原文范围；图片 ID 绑定原附件，模型只补图片区域或已提供帧的引用。Schema 的可引用值来自本次实际提供的目录，而不是所有仓库内容的巨大枚举。

模型的语义输出使用结构化分组，不依赖模型自建编号和另一个跨列表引用表：

```text
observations: [Claim]
requirement_groups: [
  { alternatives: [ { all_of: [Claim] }, ... ] }
]
frames: [Claim]

Claim:
  statement
  evidence_refs
  targets
  optional_formalization
```

一个 group 的一个 alternative 表示一个解释，该解释内的 claims 合取；单 alternative 表示没有声明歧义。程序按结构位置分配 claim/group ID，再编译到现有 MUST/MAY/FRAME 推理表示。原输出完整保留，不通过删除非法候选组或任意展平数组来修补模型输出。

普通 UI、CSS、模板、配置需求可以保留为有来源的自然语言义务；可支持的谓词再进入形式化后端。没有形式化表达不等于没有用户需求，更不能为填满字段把所有 UI 问题转成布尔函数返回值。

### 3.3 RepositoryPort：先检索，再对选中区域做可选解析

- 文件目录和文本检索是独立基础能力，不等待全仓 AST。
- 文件目录分页，文本按窗口读取；以总预算约束每次输出，不一次序列化整仓全部节点。
- 检索候选先依据 issue、路径、标识符与内容确定。可选的语法解析仅处理选中文件或区域，并返回逐文件状态。
- 解析器超限、语法不支持或原文件已有解析错误，只令该分析结果变为 PARTIAL/UNKNOWN；已有文本窗口仍可进入规划和生成。
- AST 摘要、普通文本区域、插入锚点都可以成为修复边界；文本边界不再限制为一行。
- 文本文件按可无损往返的编码处理，保留原换行、文件模式和原字节；不靠短扩展名白名单决定是否具有可编辑性。
- 机密、评测注入资产、Git 内部路径和目录逃逸属于硬安全边界；文件类型不应冒充安全边界。基础仓库文件的可读/可写清单由全实验统一策略明确记录，不因某个案例临时改变。
- 二进制文件、符号链接和文件模式属于单独声明的仓库能力。可以只登记不解引用；没有实现和验证对应操作前，覆盖报告不得宣称它们已可修复。

### 3.4 ContractResult / BoundarySet：不能用 UNKNOWN 表示“禁止生成”

所有分析结果携带：

```text
status: complete | partial | unavailable
payload
coverage
unresolved
provenance
```

这里的通用状态只描述分析可用性；具体语义判断继续使用 FEASIBLE / INEXPRESSIBLE / UNKNOWN，不能相互混淆。

- 没有可靠局部摘要：UNKNOWN，而不是整题不可修复。
- 某个受限接口有不可表达证书：仅排除该接口。允许更广的读取/写入能力，就必须视为另一个接口。
- 所有语义候选均不适用：使用通用源码检索得到的文本边界，不依赖存在某类 AST 节点。
- 证据响应无法合法编译：保留原始响应作为失败记录，返回 unavailable，不编造约束。后续在生成前选择 raw-evidence 模式，直接使用原 issue、原附件和源码；不把同一个证据请求再发给模型。
- `must=[]` 且提取失败，绝不解释成“没有修复要求”。原始任务和提取状态始终是后续输入。

### 3.5 GenerationPlan：冻结的是足够表达改动的范围，不是最小字符槽

```text
GenerationPlan
  mode: certified | scoped | raw_evidence
  writable_regions
  insertion_anchors
  allowed_creations / removals / renames
  operation_capabilities
  requirements / frames
  certificates
  unresolved
  budget
```

三种模式共用同一生成接口和补丁编译器，差别是可用的语义约束强度：

- certified：支持的局部语义与前提齐备，使用有依据的修改语法。
- scoped：有可用需求，但语义不完整；允许在预先声明的文本/语法区域完成一次多位置修改。
- raw_evidence：规格提取不可用；保留原始任务，在检索确定的范围内一次生成。明确记录研究模块未提供可用约束。

模式在任何补丁生成之前决定，不是候选失败后的第二条修复链。

范围可以涵盖函数、组件、样式块、配置对象及其关联 import/export/API 声明；可以跨文件。不能因为“一处参数比一个逻辑单元更短”就断言它足以表达修复。关联位置的发现允许有限、确定性的基础源码读取，不得使用候选补丁运行结果。

类型层面支持任意有限多区域修改，实际计划仍受统一预算和检索覆盖限制。范围太窄或位置错误依然可能影响成绩，但不会再由接口强行把所有任务压成单个小 AST 节点。

### 3.6 EditTransaction：一次输出通用编辑集合，由程序生成 diff

当前在线操作集（`edits.v4`）：

```text
replace_block(block_id, new_text)
insert_before(block_id, new_text)
insert_after(block_id, new_text)
replace_text(region_id, old_text, new_text)
create_file(path, content)
delete_file(file_id)
rename_file(file_id, destination)
```

每项 JSON 都包含 `operation / target / new_text / old_text / destination` 五字段；不使用的字段为空字符串。旧行号操作不进入新模型 Schema，也不能通过新响应解析器。

`EditRegion` 是连续阅读窗口，保留完整原文、字符偏移、原始字节范围与哈希。`EditBlock` 是程序确定的完整语法单元，具有独立 `block_id`、所属窗口、原编码字节范围、哈希与节点类别。模型选择块 ID，不再选择其起止行号或计算结束括号位置。阅读位置使用 LF 分行、一基行号和零基 Unicode 字符列号，仅作说明；源码不在嵌套块间重复显示。

TypeScript 5.8.3 只解析检索选中的文件，块目录与语义证明支持独立。每文件最多输出 4096 块并受原有元数据限额约束；冻结目录最多 128 块，按窗口分摊。完整函数/方法等单元优先，再按文本相关度、大小和位置确定性排序。只保留完整落在显示窗口内的节点，不扩大授权范围；块范围包含该节点自身的结束定界符，行首缩进和紧邻的行末换行可随块纳入。块不必可独立作为整个文件解析，例如类方法仍属于原类。

没有可用完整块的窗口，包括非支持语言、可选解析执行失败、原语法错误和截断窗口，生成前固定为 `edit_mode=text`。该模式的 `old_text` 必须逐字符精确且唯一匹配；空搜索仅对空窗口合法。插入可以把唯一原文替换为“原文加新内容”；删除使用空 `new_text`。不做模糊匹配、行号回退、括号猜补或第二次模型调用。已有块的 syntax 窗口不开放文本操作，不能绕过冻结块边界。

真实请求 Schema 枚举当前块 ID、text 窗口 ID 和完整文件 ID，未将任意字符串留作已有目标。编译器重新验证目标、哈希、原文唯一性和原子事务冲突。旧 `replace_region` 仍供 certified 布尔合成内部使用；底层行号兼容不能成为模型逃离新协议的入口。

- 一个事务可以包含多个文件、多个互不冲突的修改。
- region 可以是多行逻辑单元；通用模式不要求新代码仍是同一个 AST 节点种类。
- 修改函数签名、调用方和导入可以处在同一个事务中。
- 替换整个文件在表示能力上可表达，但只有计划明确授权且完整内容已提供时才允许；不能用通用能力取消作用域约束。
- 创建、删除、重命名都须在冻结计划中授权，不能任意越过仓库文件策略。
- 如将来声明支持二进制资源，payload 必须是明确编码的 bytes，编译器需生成并验证 Git binary diff；“字段可放 base64”本身不能算二进制修复能力已经验证。
- 多操作均基于同一个不可变 base；冲突/重叠检查是整个事务级别，禁止只保留其中合法的一部分。
- 未修改字节由程序保留，不是让模型输出“其余不变”或重新复述整个文件。

PatchCompiler 在干净隔离副本中原子应用所有操作，生成标准 Git diff，核验非空和 exact-base 可应用性。支持的语法解析可以判定错误；解析器不支持的语言只能记录 syntax=unknown，不能谎称语法已通过，也不能仅因没有解析器就把合法可应用的文本补丁删掉。原文件语法通过但新代码报错时，即使语义元数据超限仍拒绝新语法错误，并保存 `trajectory/rejected_syntax.json`（文件、原/新哈希、有限诊断代码与位置）；不写 `final.patch`，不回传模型。

## 4. 明确的阶段结束状态

```text
PatchResult
  status: patch_ready | no_patch | blocked
  instance_id / base_commit
  patch_path / patch_sha256 / unified_diff
  application_check: passed | failed | not_run
  syntax_check: passed | failed | unknown
  semantic_coverage
  generation_mode
  unresolved / diagnostics
```

`patch_ready` 表示非空、在冻结 base 上可应用的候选，不表示 resolved。对于缺少解析器的文件，syntax=unknown 可以随候选提交，但必须单独统计。

下列情况不能再直接阻断进入代码生成：没有可用布尔模型、无 AST、某语言不支持、分析输出超限、无图片、个别图片不可读、需求无法形式化。

下列情况仍必须真实报告失败：base 工作区不可获取、认证失败、真实调用预算耗尽、模型不返回可解释编辑、事务不合法、可验证的生成语法错误、安全策略冲突。不能靠空补丁、注释补丁、忽略错误编辑或写一个 `.patch` 文件来冒充成功。

单向且一次生成的系统无法在任意模型响应和基础设施状态下保证每题一定产出有效 patch。通用接口要消除的是人为限制和不必要的流程死路，而不是隐藏这些真实失败。

## 5. 通用适配的验收矩阵

验收必须区分接口覆盖、分析覆盖、patch 产出率和官方 resolved，不能只报测试总数。

| 验收项 | 要求 | 当前状态 |
|---|---|---|
| 完整任务输入 | 同一投影处理 100 dev + 480 test，答案字段不可达 | 已核验投影；不是新 v3 实现 |
| 仓库基础能力 | 全部任务镜像/base 可用性有逐题记录；文件目录不依赖 AST | 未执行完整核验 |
| 输入形态 | 无图片、GIF、无后缀图片 URL、长 issue、附件不可用 | 已实现并有合成测试；GIF 仅首帧，长 issue 为固定头尾有界投影，不是交互分页 |
| 通用源码修改 | 多文件、多行、增删重命名、CRLF/Unicode、非 JS、声明配套修改 | 已实现并有字节级真实 Git apply 测试；二进制资源和符号链接未开放 |
| 语义不支持 | UNKNOWN/PARTIAL 仍进入一次生成，准确记录适用性 | 已实现 scoped/raw_evidence；有无重复生成、冻结顺序、非法事务整题拒绝测试 |
| 真实模型 dev | 固定版本和预算，不按结果重抽样；统计逐阶段到达率、可应用 patch 产出率与失败原因 | v2、v3、v3b 各为 0/5 patch；v3c 为 4/5 patch、4 题独立评分、0 resolved；全部批次独立保留 |
| 正式测试 | 在冻结方法之后运行 test，不把评分回传进行修改；selected 分母不变 | 480 test 未执行；v3c 仅完成用户指定五题 dev 的官方评分 |
| 公平对照 | 共用 Task/Context/Repository/Edit/Patch 基础设施与总预算 | 已迁移普通对照，并覆盖全部八种模块组合；尚无完整 benchmark 对照成绩 |

580 条任务的适配清单必须保留所有未覆盖项，不能以仓库名字或 instance_id 特判补齐数字。测试覆盖应按可观察的输入与编辑形态定义，而不是复制 gold patch。

## 6. 与当前文件的契约差异

- `ports.py`：分离通用 Repository/Patch 能力与语言相关 Semantic 能力；保留三个研究模块的解耦。
- `domain/task.py`：增加来源目录、源码区域和覆盖状态，不引入答案字段。
- `kernel/evidence.py`：删除模型维护的第二套 source_id；歧义结构由嵌套表示编译，ID 由程序分配。
- `adapters/frontend.py` / `kernel/retrieval.py`：文本检索先行、按需解析；禁止把全仓 AST 作为模型调用前的必经关口。
- `domain/repair.py` / `algorithms/synthesis.py`：通用计划和多文件事务承载生成；强语义语法只是其中一种模式。
- `adapters/program.py`：语言分析和通用物化分离；exact AST-kind 匹配仅约束声明了该语法的强模式。
- `experiments/runner.py`：记录 generation_mode、coverage、application/syntax 状态与真正的终止原因；不把“该题已记录结果”当作“已生成补丁”。

当前生产主链已经按上述边界接通，不再处于仅有设计文档的状态。实际源码类型集中在 `domain/repair.py` 与 `domain/specification.py`，原 `TaskInput` 白名单保持不变。工程检查、4090 同源 Docker 检查和原五题真实回归必须分别保存结果，不能用设计、输入清单或合成测试代替真实任务验收。完整 580 个镜像/base 和全量 benchmark 修复覆盖仍未核验。
