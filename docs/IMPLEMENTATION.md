# 逐函数实现说明与适用边界

原始 A/S/L/G/C 入口均有具体实现。下面列出输入、计算与期望输出；实际函数签名见 API_REFERENCE。

## A：底层适配器

| 入口 | 算法/实现 | 期望输出与自检 |
|---|---|---|
| A1 FrozenModelAdapter.complete | 单次 HTTP/显式 fixture；用量记账；JSON/结束状态检查 | ModelResponse；本地 HTTP 实测序列化/限额/错误，真实供应商未调用 |
| A2 LogicAdapter.check | AST/类型预检、等式域收缩、完整有限枚举 | SAT 模型/UNSAT 理论摘要/UNKNOWN；不能把截断计为 UNSAT |
| A3 ProgramAdapter.index | 固定编译器 parse-only、生产文件过滤、UTF-8 span/hash | ProgramIndex；中文/emoji 字节偏移和分级 fallback 实测 |
| A4 ProgramAdapter.summarize | 纯 BooleanReturn + 完整 Boolean 参数元组 + 恒等 continuation | LocalRepairModel；仅直接函数入口的有限情境 |
| A5 ProgramAdapter.effects | 支持局部函数 return 属性，其余 wildcard/partial | Effect[]；不是全程序无干扰证明 |
| A6 ProgramAdapter.materialize | 哈希/范围/重叠校验、内存替换、重新解析、布尔义务检查、真实 diff | PatchArtifact；实测 git apply、换行、Unicode、越界和 no-op |
| A7 DockerWorkspaceAdapter.open_base | 检查实例映射/digest，受限 docker git archive，过滤解包，finally 清理 | immutable snapshot；Docker 命令模拟测试，Git archive 真正执行 |
| A8 OfficialDockerEvaluator.evaluate | 数据/预测/版本/镜像绑定，独立一次 harness，解析逐题报告 | EvaluationReport；真实 Docker/harness 未运行，不从退出码伪造 resolved |

## S：部分行为规格

S1 `extract_evidence`：一次冻结模型抽取，输出 JSON 通过严格字段检查；issue 文本定位必须是原文子串，
图片引用必须来自原 issue，并带规范化锚点；源码引用必须落在提供的 base snippet。
这校验来源与结构，不证明模型对文字/图片的语义解读正确。

S2 `bind_entities`：在索引中保留所有精确名称候选；回退词法绑定标 PARTIAL。
同名候选不以任意第一个冒充唯一绑定。未建立复杂视觉→运行时对象的一一对应证明。

S3 `build_interpretation_space`：构造 claim-acceptance 布尔变量与互斥解释组，独立记录观察、需求、frame。
无组的抽取事实视为当前声明理论中的假设；不是从像素证明事实。拒绝矛盾理论的空集蕴含。

S4 `derive_contracts`：在完整声明理论中检查蕴含/可满足，输出 MUST/MAY；不确定 frame 不变成保护义务。
Witness 的应用可达性默认 UNKNOWN。MUST 不是客观真实性分数，必须结合来源审计。

## L：局部修复可表达性

L1 `enumerate_boundaries`：和对照使用同一词法候选池；一个 BooleanReturn 有常量/参数读取两个接口。
其他 AST/文件范围保留；不以 gold 文件指导检索。

L2 `build_local_model`：把 MUST/frame 通过当前支持的恒等 continuation 反推到 local_out；
按照相同可读输入添加局部输出相等约束。当前没有一般 JS 后向符号执行。

L3 `assess_expressivity`：四项前提（读取完整、纯确定、见证入口可达、下游摘要可靠）齐备且域完整才求证。
SAT 表示当前有限约束存在局部映射；UNSAT 只排除该读取/编辑接口。UNKNOWN 保留。
`minimum_features` 对有限候选子集精确求解，输出当前见证集的最小区分特征；不是分布外最小证明。

L4 `rank_boundaries`：确定排序，保留 UNKNOWN 和被排除项的审计证书；不根据评分调整排序。

## G：属性级作用域合成

G1 `enumerate_plans`：根据边界生成有限计划/语法空洞，计入候选与思路预算；拒绝有可靠证书的不可表达接口。
当前未知 consumer/identity 等修改采用范围约束下的普通片段填充，不能当成完整专用变换编译器。
`obligations` 只有 MUST/frame；MAY 单独作为 soft_obligations。

G2 `select_minimal_scope`：按未解决需求、保持义务、保护属性风险、额外范围、未来源常量、AST 范围大小排序。
当前 before-fill invented_constants 为零（尚未生成代码）；AST 大小是待修改原语法范围，不是全程序语义距离。
未知 effects 增加风险，不等于已证明违反 frame。全部 unresolved 继续保留。

G3 `fill_holes`：支持一个布尔表达式空洞时，用有限语法和见证真值表合成 !/&&/|| 表达式；
按语义签名去重，受节点/行为/特征上限约束。无解或不支持则一次普通模型空洞填充；不根据修复后运行反馈再试。
materialize 会重验支持的布尔义务，但不验证任意渲染逻辑、跨函数回归或完整测试集。

## C：三个消融对照

C1 普通证据表示全作 MAY，不执行 entailment；C2 同候选池词法排序，不执行表达性求证；
C3 第一可用范围一次普通模型填充，不执行作用域成本排序或布尔合成。
公共 HoleRenderer 只做受限生成和 JSON 校验，不偷偷调用研究算法。
八种组合的集成测试使用明确标注的模型 double，不是八组真实 benchmark 分数。

## 仍没有实现为一般算法的部分

完整视觉 grounding 正确性、任意 UI 到内部状态可达性、CSS/Canvas/布局的后向语义、跨函数别名、
事件循环/异步状态、复杂正则完整语义、自动发现多文件耦合修改、构建生成副本同步。
这些部分进入 UNKNOWN/普通生成，而不是未实现异常；要扩展论文的机制覆盖，应增加独立语义适配器及真实任务测试。
