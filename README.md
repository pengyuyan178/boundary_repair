# BoundaryRepair — 单向局部修复

事务编辑协议为 `edits.v5`，首选授权语法块或文本窗口内的精确 SEARCH/REPLACE。证据协议为 `evidence.v6`：程序提供观测接口和有限入口情境，模型关联有来源需求，程序保留绑定歧义并产生局部见证。文本检索、最多一次生成与独立评分流程保持不变。
**工程验收不等于真实任务修复，也不等于优于 baseline。正式评分不返回生成模块。**

## 主链

```text
原始 issue / 可用原附件 / exact base snapshot
  → 有界文本检索与可选逐文件语义分析
  → 程序观测/情境目录 → evidence.v6关联 → MUST / MAY / FRAME + 程序见证与绑定备选
  → FEASIBLE / 有证书的 INEXPRESSIBLE / UNKNOWN
  → 比较完整边界及联合作用域计划（覆盖、保持风险、未知影响、写权限）
  → 冻结 generation_plan.json（候选成本、选择依据、读写范围、义务及接口建议）
  → 程序确定性投影 decision.v1（执行决策与按适用接口合并的未决条件）
  → 一次生成 → 原子编辑事务 → Git diff 与 exact-base 应用检查
  → 冻结预测 → 独立官方 Docker 评分
```

| 能力 | 实现 | 边界 |
|---|---|---|
| 证据协议 | 模型仅引用程序生成的 span/image ID；按结构分配 claim ID | 不证明视觉或语言理解正确 |
| 规格 | 保留自然语言义务、alternative 内合取与完整引用 | 无形式化支持时为 partial；非法证据为 unavailable |
| 仓库检索 | 安全文本策略、有界连续窗口、先检索后解析 | 固定检索预算，不保证找到所有相关位置 |
| 语义增强 | 逐文件 TypeScript 5.8.3 分析与显式资源上限 | 不支持、超限时 UNKNOWN，不阻止通用生成 |
| 多文件编辑 | 完整语法块 ID 替换/插入、唯一原文匹配、新建、删除、重命名 | 所有操作必须在冻结能力范围内；完整文件操作要求完整可见 |
| 补丁编译 | 同一 base、哈希与冲突校验、Git diff、干净副本应用检查 | 非空、可应用不等于语义正确 |
| 附件 | 原始缓存、SHA-256/MIME、公网安全检查、有界下载重试 | 不可用附件有记录；GIF 仅首帧，覆盖明确 partial |
| 实验 | 记录模式、语法状态、适用覆盖与所有 selected 案例 | generated 与 resolved 分开，不从分母删除失败题 |

### 生成模式

- `certified_projection`：已绑定硬义务联合通过，程序直接编译有限语法构造，并在最终编译时复核源码观察与证书。
- `guided_partial`：保留未建模需求和普通编辑能力，已验证属性成为最终事务的强制检查；不将整题标为已证明。
- `certified`：适用的有限布尔入口模型与声明语法。当前自动选择的完整生成子域为最多两个可读布尔变量；固定九节点语法覆盖其全部十六种真值表。有限合成失败会明确停止，不转调模型冒充原模式成功。
- `scoped`：有来源需求可用，但语义分析或有限生成器的支持不足。研究版根据第二层结果构造不同写权限的完整计划，比较成本后冻结一个计划；保留原检索阅读上下文，由一次模型调用产生所选权限内的事务，不是强约束合成。
- `raw_evidence`：证据响应不能合法编译或没有规范性需求。保留原始输入，生成前选择弱约束模式，只进行一次代码生成，不纠错重试证据。

普通对照共用任务、附件、文本检索、证据协议和补丁编译基础设施，不运行研究模块的证明与作用域排序。`scope_minimality_unproved` 等未决项会保留，不能把通用文本范围称为已证明的最小作用域。

## 在线协议

`evidence.v4` 输出：

```text
observations: [Claim]
requirement_groups: [{alternatives: [{all_of: [Claim]}]}]
frames: [Claim]
Claim = statement + evidence_refs + targets + formalization（允许 null）+ entry_cases
EntryCase = interface_id + inputs: [{parameter, value: bool}] + expected: bool
```

### 第一层与第二层的语义接口

- 程序通过同一次解析建立 `observation_interfaces`：只包含检索窗口中完整可见、支持纯布尔表达式的函数。每个 ID 绑定快照摘要、文件路径、函数及返回表达式范围/哈希、参数顺序；同名函数不会共享身份。显式非布尔类型、闭包读取、副作用、异步和不支持的表达式不会进入目录。
- 模型仍只提取一次证据。`targets`、`formalization` 和自然语言 `statement` 原样保留；**不会把 `visibility`、`required_behavior` 等自由文本属性改名为 `return`**。另一个字段 `entry_cases` 引用程序目录，必须完整填写所有布尔输入和期望布尔输出，并引用 issue 证据及对应代码片段。
- 这里只实现直接函数入口的 identity continuation，没有实现 UI 目标到内部返回值的反推。截图、CSS、Canvas、回调等目标不能冒充入口案例；不支持的 claim 保留 `entry_cases:[]`。观测项不能提供期望输出案例，FRAME 仍须有明确保持证据。
- MUST/FRAME 产生带接口身份的 witnesses；第二层按 **claim ID + case 序号 + 精确接口身份**连接约束，不再靠自然语言实体名相等。参数顺序规范化，缺失值不被当作相同的未知值。每一个硬案例都必须被覆盖；同一 claim 只覆盖一个案例不等于覆盖全部。MAY 不产生硬见证或剪枝。
- 任何未绑定的硬需求、缺失见证或接口版本不符，均保持 `UNKNOWN`，并记录 `unbound_hard_constraint` / `uncovered_entry_case`，不能删除困难需求后制造完整模型。非法响应仍整体拒绝，不修补字段，也不追加证据请求。
- `extraction_status=complete` 仅指记录下来的规范性解释都有类型化入口案例且接受理论完整，不是自然语言理解、GUI grounding 或需求穷尽性的证明。证书仍只适用于给定案例及声明的直接布尔入口域，明确保留 `entry_case_evidence_interpretation_not_verified`。同一组案例用于有限合成和生成后的语义复核。

旧 `evidence.v3` 读取器保留供历史资料使用，但不会把历史自由文本目标自动升级为程序绑定。在线研究版和普通对照同时使用 v4；旧离线模型 fixture 需要显式迁移，不能用重放旧结果代替新版本验证。

### 第二层与弱约束生成的接口

`scoped_localization_plan` 在生成前消费 `LocalizationResult`，建立保留宽权限的定位建议种子；第三层再由 `scoped_plans` 构造不同写权限的候选，经 `select_minimal_scope` 比较后冻结一个，而不是只给这个种子计算一次成本。

- 用源码字节范围和哈希把分析位置映射到已有 `block_id` / 文本 `region_id`。优先使用最小包围语法块；较大位置可映射到其内部已声明的块。同名函数不能串绑，多位置边界缺少任一可见映射就不被选作完整焦点。
- 有证书的 FEASIBLE 优先，其次保留 UNKNOWN 的定位顺序；首个可映射且未被证实不可表达的接口成为 `boundary_ids` 中的首选焦点。编辑块目录与阅读窗口按这个顺序呈现。全部接口分析保存在冻结计划中；实际模型请求消费下述 `decision.v1` 决策投影，而不是重读完整分析池。
- FEASIBLE 但超过有限生成器能力的接口仍进入弱约束生成，例如需读取三个布尔参数的接口。`sufficient_read_features` 表示给定案例下的一组充分读取特征，**不是唯一必要特征，也不是自由生成的独占变量白名单**。
- INEXPRESSIBLE 只产生 `avoid_this_interface`：不应重复该读取/修改接口，但所在语法块和文件仍可使用更宽的修法。全部已分析接口不可表达时，明确提示 `broader_interface_required`，不把自由事务也判为无解。没有证书的强判定在弱约束计划中降为 UNKNOWN。
- 定位建议种子保留原有全部编辑权限、UNKNOWN 备选、配套多文件修改、MUST/MAY/FRAME 与引用。随后第三层依据完整计划的比较结果收窄写权限，而不是依据局部负证书禁改文件。焦点本身不是必须触及的硬限制；空分析不虚构焦点，弱生成仍保留 `scope_minimality_unproved`，不声称自由补丁获得局部接口的证书。
- 普通生成对照不读取研究版结论，保持原提示词与编辑协议。两条路径继续使用 `edits.v4`、一次生成、原子事务及独立评分，无额外模型调用或失败反馈回路。

参考依据：[Agentless](https://arxiv.org/html/2407.01489v1) 将定位位置转为补丁生成的代码窗口；[SemFix](https://research.ibm.com/publications/semfix-program-repair-via-semantic-analysis) 用语义约束指导合成。本实现借鉴“分析必须被生成消费”的原则，不移植其算法，也不引入测试反馈、候选重试或额外研究模块。自由语法严格宽于现有局部证明域，不能套用后者的不可表达证书。

### 第三层的生成前方案选择

- **候选有不同的实际权限。** 保留原检索范围作为宽候选；以每个完整映射边界的位置集合为不可拆分单元，构造局部、包围语法块及多边界联合候选。联合构造按已绑定需求的源码覆盖进行有界贪心组合，并从不同种子保留备选，不穷举组合。不能为了缩短计划而去掉边界中的配套位置；也不从单个代码引用猜出新的“必要修改位置”。没有映射边界时只保留宽候选。
- **成本真正决定选择。** `ScopeCost` 改为依次比较：漏掉的已绑定 MUST 位置所属需求数、对未映射需求收窄权限的风险、未映射需求数、直接触及的 FRAME 数、未映射 FRAME 数、影响摘要是否未知、整文件/新建目录权限数、未被 MUST 位置覆盖的可写原文字节、总可写原文字节。相同成本先用定位顺序，再用稳定计划 ID，不用输入枚举顺序。移除了生成前无法得知的“新增常量数”和旧 AST 大小伪指标。
- **未知不是零风险。** 没有可靠入口绑定的硬需求，或根本没有 MUST 时，收窄权限会增加风险，优先保留宽范围。FRAME 仅按快照绑定位置与写范围的直接重叠计可能影响，不宣称已违反保持，也不从不同上下文推导互不影响。自由语法的传递影响仍为 UNKNOWN；`touched_frames=0` 不等于保持已经证明。MAY 保留，但不作为必须覆盖的硬位置。
- **读权限与写权限分离。** `read_scope` 保留原检索窗口；`edit_scope` 才决定块 ID、文本窗口、完整文件操作与新建目录权限。窄计划同时关闭未选文本窗口、完整文件删除/重命名及新建权限；未选源码仍只读可见。Schema 和编译器使用同一所选范围，兼容的整窗口编辑也不能绕过 syntax 块限制。
- **候选比较先于唯一生成。** 每个不同权限计划计一个 candidate，同一 FREEFORM 编辑语法计一个 idea；相同权限的重复接口不重复计候选。预算紧张时先保留宽候选及联合候选，触及上限有诊断，不扩预算或重试。有限生成器不支持的 FEASIBLE 接口不消耗强生成计划配额后挤掉弱计划。`generation_plan.json` 保存所有候选的 `scope_comparison`（范围、成本、未映射/未覆盖需求、保持风险）、胜者和 `selection_policy`；实际请求包含胜者的 `scope_selection`。

这实现的是**有界候选池中、基于源码干预覆盖与保守权限风险的选择**，不是全程序语义最小性证明。源码位置可修改不等于需求已满足；原文字节是编辑暴露的末位代理，不是补丁行数或行为影响的精确度量。边界及需求绑定不完整时，选择器可能仍保留宽范围；不以强行收窄制造“第三层有效”的表象。全部硬义务和未决项仍传到生成，评分不参与选择。

### 面向 benchmark 的决策交接 harness

交接实现复用现有 `ScopeSynthesis → freeze_plan → TransactionRenderer → compile` 和独立评分运行器，不增加研究模块或模型总结回合。生产改动集中在 `algorithms/rendering.py`：`generation_handoff(plan)` 是无文件读取、无模型调用的确定性投影，由唯一生成请求直接消费。

- **审计与执行分开。** `trajectory/generation_plan.json` 仍完整保存所有接口、原始未决项、多位置映射和候选成本比较。请求中 `repair_guidance.handoff_version=decision.v1`，`audit_ref` 指向该记录，供离线审计使用；不授予模型访问轨迹、工具或评分数据的权限。
- **只展开会影响当前执行的接口。** `decision_interfaces` 保留全部首选接口，以及完整映射到所选范围内的有证书接口；每项保留精确源码身份、修改语法、读取特征、有限证书及完整编辑目标组。不按字符预算截断多位置组。未选 UNKNOWN 和未映射接口只在 `deferred_interfaces` 中按判定及映射状态登记 ID，详细分析留在审计记录；它们没有被第二层或第三层候选池删除。
- **未决条件无损合并。** `unresolved_conditions=[{boundary_ids,reasons}]` 中每种原因只写一次，共享相同适用 ID 集合的原因归为一组。全局 `unresolved` 仅去掉能与这些结构化记录精确对应的 `boundary:<id>:<reason>` 重复项；未知全局诊断、提取失败、未绑定硬需求和所有硬约束 ID 保留。UNKNOWN 仍无否定结论，局部负证书不扩张成文件禁改。
- **不是截短任务来换连通率。** 原 issue、附件引用、阅读源码、可写文件/块/文本窗口、新建目录、MUST/FRAME/MAY、解释组、精确案例身份与来源全部不变。所选方案、成本顺序、Schema 和编译权限不变；有证书接口很多时仍可能产生较长请求，没有声明统一 token 硬上限。
- **实验协议不变。** `evidence.v4 + edits.v4` 输出协议、seed、预算、一次证据/代码生成、冻结预测后独立评分均不变。普通对照不接收研究版 `repair_guidance`，保留原提示字段及顺序。压缩不是额外修复候选，不计作新一轮生成；不使用评分反馈重新选择方案。

专项测试：`python -m unittest discover -s tests -p test_decision_handoff.py -v`。离线重放脚本为 `verification/decision_handoff_20260911/replay.py`，只读取指定批次的生成侧 `task.json`、`generation_plan.json` 和 `edits.v4.request.json`，不读模型补丁、官方测试或评分数据，不发送网络请求。

原三层批次中已有的全部五份代码请求离线重放显示：总提示字符减少 **27.10%–41.37%**；接口建议从 30,073–36,740 字符降至 2,904–3,530 字符。五份冻结计划均保留 40 个接口，请求均只展开 1 个首选 UNKNOWN；全部未决条件的适用关系精确相等，其余请求字段及输出 Schema 相等。完整记录见 [prompt_replay.json](verification/decision_handoff_20260911/prompt_replay.json)。这不是五题新实验，不是输入 token 实测，也不能证明 HTTP 503 根因已解决或 resolved 提升。

`edits.v4` 输出一个完整事务，每个操作具有 `operation / target / new_text / old_text / destination`：

- `replace_block`：模型选择 `block_id` 并返回该完整语法单元；起止字节、结束括号及原文哈希由程序确定。
- `insert_before / insert_after`：在块的固定边界插入，必要换行和分隔符由 `new_text` 提供。
- `replace_text`：仅对生成前声明为 `edit_mode=text` 的窗口开放；`old_text` 必须在该窗口中唯一精确匹配。找不到或重复（包括重叠重复）均拒绝，不模糊匹配。空搜索仅适用于空窗口。
- `create_file / delete_file / rename_file`：受冻结目录和完整文件权限限制。

模型不输出 diff，也不填写行号。阅读窗口和编辑块目录分离：源码只显示一份；块的位置只用于阅读，不能作为可编辑参数。只对检索选中的文件使用现有 TypeScript 5.8.3；每文件最多提取 4096 块，冻结目录总计最多 128 块，按窗口分摊并确定性排序。块必须完整落在可见窗口内。解析不可用、原语法错误或窗口内没有可用完整块时，在生成前固定使用原文匹配，不是失败后重试。

真实请求的 Schema 动态枚举允许的块/窗口/完整文件 ID；编译器再次检查哈希、唯一匹配、重叠和文件权限，并原子应用整个事务。新增语法错误保存为 `trajectory/rejected_syntax.json`，不冒充 `final.patch`，不反馈给模型。旧 `replace_region / replace_lines / insert_at` 只保留底层兼容和 certified 内部用途；新模型 Schema 和响应解析器不接受它们。

默认 `integration.response_format="json_schema"`；模型服务不支持时明确报错，不自动降级或重试。

## 工程验证

Python 3.11+、Git、Node.js、固定 TypeScript 5.8.3；GIF 需要 Pillow，测试还需要 jsonschema。

```powershell
$env:PYTHONUTF8='1'
python -m pip install -e '.[test]'
npm.cmd ci --ignore-scripts --no-audit --no-fund
python -m unittest discover -s tests -v
python -m unittest discover -s tests -p test_evidence_alignment.py -v
python -m unittest discover -s tests -p test_scoped_alignment.py -v
python -m unittest discover -s tests -p test_scope_selection.py -v
```

smoke 使用明确标注的合成证据 fixture，生成真实 diff 后独立执行 Git/Node 检查。它不是 benchmark 成绩，也没有真实模型调用。

### 2026-09-11 第三层方案选择验收

- Windows 本地完整回归：**306 项，304 通过、2 项 POSIX-only 跳过，0 失败**（117.715 秒）。新增 `test_scope_selection.py` 的 28 项全部通过；八种消融组合、前两层接口、有限布尔合成、Git 应用与单次生成回归通过。
- 固定源码、MUST 与第二层多位置候选，真实枚举器产生 5 个不同权限计划。没有额外保持要求时选择 33 字节范围；增加对 `small.js` 的 FRAME 后，改选 50 字节范围，避开该保持位置。两个计划的传递影响都仍为 UNKNOWN。这验证成本能战胜“选更短范围”，不把未知伪造成无风险；对应[合成决策记录](verification/scope_selection_20260911.decision_check.json)。
- 验证完整多位置边界不被截断、跨位置需求的联合计划、未映射硬需求保留宽范围、MAY 不扩大硬覆盖、稳定并列排序、预算计数、比较先冻结后生成、真实模型请求 Schema 变化、只读上下文保留，以及编译器拒绝未选块、整窗口旁路、删除、重命名与新建越界操作。脚本模型不是实际模型能力实验。
- [最终完整日志](verification/scope_selection_20260911.final_tests.log)与[63 个源码/测试文件的 SHA-256 清单](verification/scope_selection_20260911.source_hashes.json)对应本批次。相对上一批次修改五个生产文件、调整两个既有测试文件、新增一个测试文件，没有新增研究模块。首次完整回归的测试桩错误保留在 `scope_selection_20260911.tests.log`，不作为成功日志。
- Python 3.13.13、Node v26.1.0、TypeScript 5.8.3；Python 3.11 语法检查通过，但不是 Python 3.11 运行时或 4090 Docker 验收。未调用真实模型、未访问服务器、未重跑 benchmark，未读取 gold/test patch 作为算法输入。

**证据边界：**修复了“单个计划只有成本记录、没有方案选择”的工程和算法连接缺陷。当前只在受支持的入口绑定与声明候选中计算干预覆盖、保持位置风险及写权限暴露；不证明一般 GUI 修复的行为最小性、真实 resolved 提升或优于 baseline。未映射需求导致保留宽范围，是显式限制，不算已经取得作用域优化收益。

### 2026-09-11 第二层到弱约束生成验收（历史批次）

- Windows 本地完整回归：**278 项，276 通过、2 项 POSIX-only 跳过，0 失败**（98.000 秒）。运行环境为 Python 3.13.13、Node v26.1.0、TypeScript 5.8.3；Python 3.11 语法检查通过，但未运行 Python 3.11 或服务器 Docker 验收。
- 新增 `test_scoped_alignment.py` 的 21 项全部通过。固定源码和第一层需求，移除第二层结果后，冻结计划和实际模型请求不再相同；调换 UNKNOWN 候选顺序会改变首选块及阅读窗口顺序，按焦点工作的脚本生成器产生不同位置的真实可应用补丁。
- 真实解析器和逻辑后端验证了三布尔输入的可行接口进入一次弱约束生成，充分读取特征保留；实际 Git apply 与 Node 执行检查通过。常量接口不可表达时，仍允许同一块使用更宽读取接口修复，且不删除 UNKNOWN 备选、配套编辑权限或未映射硬需求。
- 覆盖同文件/跨文件同名函数、旧哈希、旧行范围、不可见多位置边界、未检索路径不读取、无证书判定降级、生成前冻结、非法响应不重试。普通生成对照的实际请求不随第二层结论变化，保持消融独立性。
- [最终完整测试日志](verification/scoped_alignment_20260911.final_tests.log)与[62 个源码/测试文件的 SHA-256 清单](verification/scoped_alignment_20260911.source_hashes.json)对应本批次。相对上一批次只修改了三个生产文件，并新增一个跨层测试文件；没有新增研究模块。

**证据边界：**这证明了定位结果对弱生成计划及输入的实际作用，不证明真实模型一定遵循建议。脚本生成器的补丁差异不是模型收益，局部分析也没有成为自由事务的强制语义验证器。全部弱编辑权限有意保留，未声称最小作用域、真实修复率提升或优于 baseline。本轮未调用真实模型、未访问服务器、未重跑 benchmark，未使用 gold/test patch 作为算法输入。

### 2026-09-11 第一层到第二层验收（历史批次）

- Windows 本地完整回归：**257 项，255 通过、2 跳过，0 失败**（67.971 秒）。两项跳过均为 POSIX Git 文件模式/umask 检查，不计为通过；本轮未在 4090 Docker 上运行。
- 新增的 `test_evidence_alignment.py` 共 22 项全部通过，使用真实 TypeScript 前端、逻辑后端和合成链路；覆盖全部 16 种双布尔输入真值表的接口判定与最小读取特征，并实际执行 Git apply 和 Node 行为检查。
- 验收包含反例：移除程序绑定、漏掉硬案例、混入不支持的硬需求、同名函数串绑、旧快照及非法输入均不能获得完整证书；MAY 不参与硬剪枝，错误生成结果被语义复核拒绝。原自然语言目标保持不变。
- 运行环境为 Python 3.13.13、Node v26.1.0、TypeScript 5.8.3。Python 3.11 语法检查通过，但不是 Python 3.11 运行时验收。
- [该批次完整测试日志](verification/evidence_alignment_20260911.final_tests.log)与[61 个源码/测试文件的 SHA-256 清单](verification/evidence_alignment_20260911.source_hashes.json)标识弱生成对齐修改前的版本，不代表后续源码。`evidence_alignment_20260911.tests.log` 保留的是修正前失败记录，不是该批次最终验收结果。

这组测试证明支持域内的第一层案例被第二层及 `certified` 生成消费，未覆盖第二层到 `scoped` 生成的消费关系，也不证明模型能从真实 GUI issue 正确提取案例或历史十题的修复率改善。该批次没有真实模型调用、benchmark 重跑或官方评分。

v3 验证记录在 `verification/20260911_interface_v3/`，与 v2 的 `verification/20260910_local_edits/` 分开；各批次原始测试输出与源码清单以实际产物为准。

## 4090 正式环境

真实实验使用 4090 服务器 Docker exact-base 工作区和独立官方评分。只读取服务器现有 `.env` 中预先配置的变量；不将密钥复制进配置、源码包或产物。

```bash
python run.py doctor --config configs/server_docker.json
python run.py inspect --config configs/server_docker.json
```

用户指定的原五题回归保持 seed=42、GPT-4.1 与原预算，不换题、不依据官方结果重生成。**v3c 已实际走完生成、补丁提交和独立官方评分：5 题尝试、4 题产出非空可应用 patch、4 题评分、0 题 resolved。** 四份补丁均通过实际 Git 应用和语法检查，其中一份包含新建 helper 与原文件修改；marked 一题因生成语法错误未产出补丁，保留在分母中。

| 开发回归版本 | 非空 patch | 实际官方评分 | resolved |
|---|---:|---:|---|
| v2 | 0/5 | 0 | 未评分 |
| v3 | 0/5 | 0 | 未评分 |
| v3b | 0/5 | 0 | 未评分 |
| v3c | 4/5 | 4 | 0/5 |

各版本使用独立冻结源码和批次目录，不覆盖旧失败，也不将多轮成绩拼成一个结果。v3c 修正 Git 模式比较和普通省略号注释误报后，Windows 完整验收为 211 passed、2 项 POSIX-only skipped，4090 Docker 在 `umask=0002` 下为 213 passed、0 skipped；两端 smoke 均通过。真实 patch 产出链路已跑通，但本轮没有证明修复能力有效或优于 baseline。

本轮完整报告：[v3c 五题结果](../../result/method/boundary_repair/dev_random5_gpt41_seed42_v3c_20260910_181639_UTC/REPORT.md)。历史 v2、v3、v3b 报告分别保留在项目 `result/method/boundary_repair/` 的对应版本批次中。完整 100 dev 与冻结 480 test 未运行。

## 仍未证明的能力

一般视觉 grounding、UI 到内部状态可达性、CSS/Canvas 后向语义、跨函数别名、异步状态与完整关联位置发现仍不完整。通用多文件事务解决表达和物化接口，不自动解决这些分析问题。符号链接和任意二进制资源编辑尚未作为通用操作开放；部分语法检查只能报告 `unknown`。

完整设计与实现偏差见 [INTERFACE_V3_DESIGN.md](docs/INTERFACE_V3_DESIGN.md)。旧 `IMPLEMENTATION.md`、`API_REFERENCE.md` 和历史成绩是对应版本记录，不作为新版端到端验收证据。
# 第4项机制实现

最新独立实现说明见 [三层机制执行链](docs/MECHANISM_ACTIVATION.md)。它继承 `2ce14ad` 的隔离和编辑协议修复，使用程序情境目录与 `evidence.v6` 关联；部分已证明义务可以约束最终事务。本轮只进行工程回归和历史证据离线重放，dev实验等待五类问题全部完成。
