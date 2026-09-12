# 逐函数实现说明与适用边界

下文 A/S/L/G/C 是历史实现说明；**当前事务编辑协议为 edits.v5**，证据协议沿用现有配置，不是下文保留的 v2 签名。当前接口设计见 [INTERFACE_V3_DESIGN.md](INTERFACE_V3_DESIGN.md)。

## 当前编辑边界实现（2026-09-11）

- `frontend/parse.cjs` 复用 TypeScript 5.8.3 的 `getStart/end`，提取完整声明、方法、构造器、访问器和语句；最多 4096 个块，并计入元数据上限。语法可解析不等于语义可证明。
- `ProgramAdapter.source_scope` 先检索，再只解析选中文件，并缓存结果供 `index` 使用。`bind_edit_blocks` 将 UTF-8 解析偏移转换到原文件编码，验证哈希，只冻结完整落在阅读窗口内的最多 128 个块；不扩大窗口。
- `EditRegion` 只承载阅读窗口；`EditBlock` 承载原字节范围和哈希。目录可以包含嵌套块，但同一事务不能同时替换父子块。
- `TransactionRenderer` 只调用一次 `edits.v5`，首选授权块或文本模式窗口内的精确 SEARCH/REPLACE。没有可填写的起止行号；源码窗口保留一份，块目录补充首尾各至多160字符的精确片段、长度与哈希以说明分隔符归属。片段可能重叠，不拼接为完整块。
- `transaction_contents` 针对同一原始 base 原子计算全部替换、插入和文件操作。原文匹配不模糊、不修正、不默认取第一个；未知 ID、重叠匹配、哈希变化均拒绝。
- `PatchCompiler` 保留语法检查、Git diff、exact-base 应用及字节/模式核验。新增语法错误仅保存 `trajectory/rejected_syntax.json` 诊断，不产出 `final.patch`，不回传模型。
- certified 有限布尔合成仍由程序使用内部 `replace_region`；旧行号操作只保留底层兼容，不在新模型 Schema 或响应解析器中开放。八种消融组合共享上述接口基础设施。

以下保留历史说明，不将其旧协议与当前在线操作混用。

## A：底层适配器

| 入口 | 算法/实现 | 期望输出与自检 |
|---|---|---|
| A1 FrozenModelAdapter.complete | 单次 HTTP/显式 fixture；严格 Schema/本地校验；用量记账；附件缓存 | ModelResponse；loopback 实测新请求契约，真实供应商 v2 兼容性仍需联调 |
| A2 LogicAdapter.check | AST/类型预检、等式域收缩、完整有限枚举 | SAT 模型/UNSAT 理论摘要/UNKNOWN；不能把截断计为 UNSAT |
| A3 ProgramAdapter.index | 固定编译器 parse-only、生产文件过滤、UTF-8 span/hash | ProgramIndex；中文/emoji 字节偏移和分级 fallback 实测 |
| A4 ProgramAdapter.summarize | 纯 BooleanReturn + 完整 Boolean 参数元组 + 恒等 continuation | LocalRepairModel；仅直接函数入口的有限情境 |
| A5 ProgramAdapter.effects | 支持局部函数 return 属性，其余 wildcard/partial | Effect[]；不是全程序无干扰证明 |
| A6 ProgramAdapter.materialize | 哈希/范围/重叠校验、内存替换、重新解析、布尔义务检查、真实 diff | PatchArtifact；实测 git apply、换行、Unicode、越界和 no-op |
| A7 DockerWorkspaceAdapter.open_base | 检查实例映射/digest，受限 docker git archive，过滤解包，finally 清理 | immutable snapshot；旧随机十题批次已使用真实 Docker，当前验证单独记录 |
| A8 OfficialDockerEvaluator.evaluate | 数据/预测/版本/镜像绑定，独立一次 harness，解析逐题报告 | EvaluationReport；旧随机十题官方 resolved=0/10，不是 v2 成绩 |

## S：部分行为规格

S1 `extract_evidence`：一次冻结模型抽取。`evidence_spans` 将 issue 与源码窗口分成原文行，
超长行按 2000 Unicode 字符连续切块；保留 CRLF、URL 编码和重复段，源码偏移对应完整原文件。
模型只选择 span_id，程序解析为原文 locator；不模糊匹配、不拼接引用、不修补非法候选组或逻辑数组。
图片引用使用原附件 asset_id 和四个归一化 bbox 数值。`evidence.v2` 的正式递归 Schema 与
本地 canonical term/必需字段检查一致；本地另外验证 ID 存在性、来源匹配及跨字段约束。
这校验来源与结构，不证明模型对文字/图片的语义解读正确。

S2 `bind_entities`：在索引中保留所有精确名称候选；回退词法绑定标 PARTIAL。
同名候选不以任意第一个冒充唯一绑定。未建立复杂视觉→运行时对象的一一对应证明。

S3 `build_interpretation_space`：构造 claim-acceptance 布尔变量与互斥解释组，独立记录观察、需求、frame。
无组的抽取事实视为当前声明理论中的假设；不是从像素证明事实。拒绝矛盾理论的空集蕴含。

S4 `derive_contracts`：在完整声明理论中检查蕴含/可满足，输出 MUST/MAY；不确定 frame 不变成保护义务。
Witness 的应用可达性默认 UNKNOWN。MUST 不是客观真实性分数，必须结合来源审计。

## L：局部修复可表达性

L1 `enumerate_boundaries`：和对照使用同一候选池；按长度归一化词法相关度、路径与符号匹配排序。
源码上下文围绕命中点取连续原文窗口，不再只取文件头。新增调用参数/初始化值等局部语法位置，
不等于完整调用图或符号解析。一个 BooleanReturn 有常量/参数读取两个接口；不以 gold 文件指导检索。

L2 `build_local_model`：把 MUST/frame 通过当前支持的恒等 continuation 反推到 local_out；
按照相同可读输入添加局部输出相等约束。当前没有一般 JS 后向符号执行。

L3 `assess_expressivity`：四项前提（读取完整、纯确定、见证入口可达、下游摘要可靠）齐备且域完整才求证。
SAT 表示当前有限约束存在局部映射；UNSAT 只排除该读取/编辑接口。UNKNOWN 保留。
`minimum_features` 对有限候选子集精确求解，输出当前见证集的最小区分特征；不是分布外最小证明。

L4 `rank_boundaries`：确定排序，保留 UNKNOWN 和被排除项的审计证书；不根据评分调整排序。

## G：属性级作用域合成

G1 `enumerate_plans`：根据边界生成有限计划，计入候选与思路预算；拒绝有可靠证书的不可表达接口。
大边界在生成前拆成不重叠局部语法位置，每个最多 128 AST 节点，每计划最多 64 个位置；
按确定的相关度/范围排序选择，不依据生成失败或评分改变。这些是实现的结构预算，不是语义证明。
不再把 File/Function/带函数体的 Assignment 整段交给模型重写。找不到位置的计划不可用，
但该题仍计入 selected；非 JS 支持源文件仅有原文 TextLine 弱路径，禁止跨行替换。
`obligations` 只有 MUST/frame；MAY 单独作为 soft_obligations。

G2 `select_minimal_scope`：按未解决需求、保持义务、保护属性风险、额外范围、未来源常量、AST 范围大小排序。
当前 before-fill invented_constants 为零（尚未生成代码）；AST 大小是待修改原语法范围，不是全程序语义距离。
未知 effects 增加风险，不等于已证明违反 frame。全部 unresolved 继续保留。

G3 `fill_holes`：支持一个布尔表达式空洞时，用有限语法和见证真值表合成 !/&&/|| 表达式；
按语义签名去重，受节点/行为/特征上限约束。无解或不支持则一次 `fillings.v2` 局部编辑请求。
模型只提交需要改变的位置，未提交位置由程序保留。替换内容必须完整；仅 `local:Statement`
允许显式 delete，不以删行数阈值拒绝合法删除。源码外围空白仅在非 TextLine 的新生成片段边缘去除。
Consumer 只能 guard_consumer；原 fallback 由程序拼接，并在重新解析后独立核验。

materialize 核对编辑后的精确 AST 范围，包含布尔路径，拒绝通过闭合括号/函数体注入兄弟代码；
拒绝隐式删除、省略占位注释和已解析 JS 文件的 token 无变化补丁。语法指纹保留 JSX 文本和选定编译指令，
不是语义等价证明；TextLine 不享有 JS token/AST 保障。支持的布尔义务继续检查；未知渲染逻辑、
条件副作用、别名或完整回归不被宣称已经证明。所有失败仅记录，不请求模型修正或获取官方反馈。

## C：三个消融对照

C1 普通证据表示全作 MAY，不执行 entailment；C2 同候选池词法排序，不执行表达性求证；
C3 第一可用范围一次普通模型填充，不执行作用域成本排序或布尔合成。
公共 HoleRenderer 只做受限生成和 JSON 校验，不偷偷调用研究算法。
八种组合的集成测试使用明确标注的模型 double，不是八组真实 benchmark 分数。

## 仍没有实现为一般算法的部分

完整视觉 grounding 正确性、任意 UI 到内部状态可达性、CSS/Canvas/布局的后向语义、跨函数别名、
事件循环/异步状态、复杂正则完整语义、自动发现多文件耦合修改、构建生成副本同步。
这些部分进入 UNKNOWN/普通生成，而不是未实现异常；要扩展论文的机制覆盖，应增加独立语义适配器及真实任务测试。
