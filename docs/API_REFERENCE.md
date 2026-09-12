# 函数 API 与注释索引

这是 v0.2.0 初次交付时由 scripts/audit_functions.py 生成的历史快照，保留当时的覆盖证据，不代表当前签名或覆盖率。当前签名以 src/ 源码为准，覆盖只是执行证据，不是正确性证明。

## 当前在线编辑接口（2026-09-11）

当前事务编辑使用 `edits.v5`；证据使用 `evidence.v6` 的程序情境关联协议，见文末补充及 `MECHANISM_ACTIVATION.md`。下文原函数索引保留为历史记录。

```python
ProgramAdapter.source_scope(snapshot, context, query='') -> EditScope
bind_edit_blocks(snapshot, scope, analysis, query, maximum=128) -> EditScope
ProgramAdapter.freeze_plan(plan, context) -> None
edit_transaction_schema(scope=None) -> dict
TransactionRenderer.render(task, plan, context) -> EditTransaction
parse_transaction(text) -> EditTransaction
transaction_contents(snapshot, scope, transaction, max_file_bytes=512000) -> tuple
PatchCompiler.compile(task, plan, transaction, snapshot, context) -> PatchArtifact
```

`EditScope.blocks` 为 `EditBlock(block_id, region_id, start_byte, end_byte, sha256, node_kind, symbol)` 的不可变元组；字节区间左闭右开且绑定原文件编码。`EditRegion.edit_mode` 在生成前固定为 `syntax` 或 `text`。

模型操作只接受 `operation / target / new_text / old_text / destination` 五字段。首选 `replace_text`：target 为已授权块 ID 或文本模式窗口 ID，old_text 须在该目标内唯一精确匹配。syntax 阅读窗口的 ID 不能授予全文替换权限。`replace_block / insert_before / insert_after` 保留兼容；完整文件操作仍受冻结权限限制。`SourceEdit.first_line/last_line` 是内部历史兼容字段，不是当前模型参数。完整契约与限制见 [INTERFACE_V3_DESIGN.md](INTERFACE_V3_DESIGN.md)。

## src/boundary_repair/adapters/dataset.py

### project_task

```python
project_task(record: dict[str, object]) -> TaskInput
```

原始行 → TaskInput；绝不透传 raw record、hints、patch、test_patch 或评分字段。

支持 image_assets 为对象或 JSON 字符串；只取 problem_statement URL 列表。
缺失附件字段表示没有结构化引用，算法仍可分析原 issue 文本；未知附件形状报错。
此函数仅做数据隔离，不下载图片、不执行图片内容、不读取 patch 图片。

状态：concrete_implementation；语句执行：13/18。

### load_tasks

```python
load_tasks(path: Path) -> tuple[TaskInput, ...]
```

JSON 数组、instance_id 映射或 JSONL → 去重校验后的不可变任务序列。

映射键必须和行内 instance_id 一致；未知 wrapper 不猜测。JSONL 按扩展名识别，
不因解析失败就换一种格式。原始行仅活在此边界内；这是 API 隔离，不是进程沙箱。

状态：concrete_implementation；语句执行：23/26。

### select_tasks

```python
select_tasks(tasks: tuple[TaskInput, ...], *, repo: str | None=None, instance_ids: tuple[str, ...]=(), limit: int | None=None) -> tuple[TaskInput, ...]
```

稳定筛选，不抽样调参；显式实例不存在或与 repo 筛选冲突时立即报错。

状态：concrete_implementation；语句执行：3/7。

## src/boundary_repair/adapters/frontend.py

### resolve_node

```python
resolve_node(config: ExperimentConfig) -> str
```

Resolve explicit Node files/directories, then PATH; never execute a target repo binary.

状态：concrete_implementation；语句执行：7/9。

### collect_sources

```python
collect_sources(snapshot: RepositorySnapshot, config: ExperimentConfig) -> tuple[list[dict[str, str]], tuple[str, ...]]
```

Read bounded regular UTF-8 production sources, recording every coverage truncation.

状态：concrete_implementation；语句执行：16/24。

### parse_sources

```python
parse_sources(files: list[dict[str, str]], config: ExperimentConfig, context: RunContext) -> dict[str, Any]
```

Parse source strings through a pinned, trusted compiler; text mode is explicitly partial.

状态：concrete_implementation；语句执行：13/19。

### retrieval_score

```python
retrieval_score(query: str, path: str, source: str) -> float
```

Deterministic lexical relevance: path/identifier matches outweigh repeated body tokens.

状态：concrete_implementation；语句执行：6/6。

### source_context

```python
source_context(snapshot: RepositorySnapshot, query: str, config: ExperimentConfig) -> list[dict[str, str]]
```

Rank raw base files and cap characters before model calls; never include tests or history.

状态：concrete_implementation；语句执行：10/10。

## src/boundary_repair/adapters/logic.py

### LogicAdapter.check

```python
LogicAdapter.check(self, assertions: tuple[Term, ...], context: RunContext) -> SolverAnswer
```

Validate all branches, enumerate the declared domain, and hash the exact checked theory.

SAT requires a model; UNSAT requires exhaustive coverage. Mixed sorts, unsupported ASTs,
node/assignment limits and deadline expiry return UNKNOWN, never a negative certificate.

状态：concrete_implementation；语句执行：28/31。

## src/boundary_repair/adapters/model.py

### read_model_environment

```python
read_model_environment(config: ExperimentConfig) -> dict[str, str]
```

Read only configured env names at call time; no expansion, execution or secret logging.

状态：concrete_implementation；语句执行：21/29。

### _public_address

```python
_public_address(host: str, port: int) -> str
```

Resolve once and require every returned address to be globally routable.

状态：concrete_implementation；语句执行：5/8。

### PinnedHTTPSConnection.__init__

```python
PinnedHTTPSConnection.__init__(self, host: str, address: str, timeout: float) -> None
```

Keep a single resolved address to prevent a second DNS lookup/rebinding race.

状态：concrete_implementation；语句执行：2/2。

### PinnedHTTPSConnection.connect

```python
PinnedHTTPSConnection.connect(self) -> None
```

Establish TCP to pinned IP, then verify TLS for the requested public hostname.

状态：concrete_implementation；语句执行：6/6。

### prepare_asset

```python
prepare_asset(asset: IssueAsset, config: ExperimentConfig, context: RunContext) -> tuple[str, str]
```

Download only direct public HTTPS images, no redirects; return data URI and content hash.

状态：concrete_implementation；语句执行：20/24。

### chat_endpoint

```python
chat_endpoint(endpoint: str, allow_local_http: bool) -> tuple[str, str, int, str]
```

Normalize an explicitly configured API base, never guessing a provider or model.

状态：concrete_implementation；语句执行：10/10。

### FrozenModelAdapter.complete

```python
FrozenModelAdapter.complete(self, request: ModelRequest, context: RunContext) -> ModelResponse
```

Send one JSON-mode multimodal chat request and require accounted usage and normal stop.

Structured output remains untrusted and is validated by each algorithm. Missing usage
charges the entire reservation then fails. Fixture outputs are explicitly synthetic.

状态：concrete_implementation；语句执行：36/44。

### FrozenModelAdapter._fixture

```python
FrozenModelAdapter._fixture(self, request: ModelRequest, context: RunContext) -> ModelResponse
```

Read a schema-keyed recorded response solely for explicit offline integration tests.

状态：concrete_implementation；语句执行：11/14。

## src/boundary_repair/adapters/process.py

### run_process

```python
run_process(arguments: list[str], *, timeout: float, cwd: Path | None=None, input_data: bytes | None=None, max_output: int=16000000, environment: Mapping[str, str] | None=None, output_file: Path | None=None) -> ProcessResult
```

Execute one command, kill its group on deadline, and cap captured output after completion.

Temporary files avoid pipe deadlocks and unbounded RAM. OS/container quotas are still needed
for adversarial disk output. Windows uses taskkill for process descendants on timeout.

状态：concrete_implementation；语句执行：21/28。

## src/boundary_repair/adapters/program.py

### ProgramAdapter.index

```python
ProgramAdapter.index(self, snapshot: RepositorySnapshot, context: RunContext) -> ProgramIndex
```

Index production sources with exact spans; include explicit partial file fallbacks.

状态：concrete_implementation；语句执行：21/21。

### ProgramAdapter.function_summary

```python
ProgramAdapter.function_summary(self, site: SourceSpan, snapshot: RepositorySnapshot, context: RunContext) -> dict[str, Any] | None
```

Return a summary only for the exact parser-certified Boolean-return byte range.

状态：concrete_implementation；语句执行：6/6。

### ProgramAdapter.summarize

```python
ProgramAdapter.summarize(self, boundary: RepairBoundary, witnesses: tuple[Witness, ...], snapshot: RepositorySnapshot, context: RunContext) -> LocalRepairModel
```

A4: construct z_b and identity continuation for pure Boolean entry functions only.

The proof universe is direct function-entry invocations with supplied Boolean arguments,
not reachability from the application UI. Unsupported closures, mutation, async code,
incomplete arguments or other output projections retain PARTIAL/UNKNOWN.

状态：concrete_implementation；语句执行：28/35。

### ProgramAdapter.effects

```python
ProgramAdapter.effects(self, plan: PatchPlan, snapshot: RepositorySnapshot, context: RunContext) -> tuple[Effect, ...]
```

A5: return a local property effect where justified, otherwise explicit unknown effects.

Identity-return edits have a precise local return projection, not a proof of whole-program
noninterference. Consumer edits and object sharing currently use conservative partial
effects. Empty impact is never inferred from unsupported analysis.

状态：concrete_implementation；语句执行：9/10。

### ProgramAdapter.materialize

```python
ProgramAdapter.materialize(self, task: TaskInput, plan: PatchPlan, fillings: tuple[HoleFilling, ...], snapshot: RepositorySnapshot, context: RunContext) -> PatchArtifact
```

A6: verify exact hashes, nonoverlap, UTF-8 and syntax; build diff without touching base.

The patched buffers are reparsed but never executed. Tests, secret paths and generated
copies are rejected by the edit policy. No-newline markers preserve valid Git patches.

状态：concrete_implementation；语句执行：72/81。

## src/boundary_repair/adapters/storage.py

### safe_component

```python
safe_component(value: str) -> str
```

校验 batch/instance/产物单级名称；拒绝路径穿越、绝对路径和 Windows 保留名。

状态：concrete_implementation；语句执行：3/3。

### json_value

```python
json_value(value: object) -> object
```

转换已知数据结构为 JSON 值；不回退到 repr，避免隐式保存密钥/客户端对象。

状态：concrete_implementation；语句执行：13/15。

### write_json

```python
write_json(path: Path, value: object) -> None
```

先完整序列化，再同目录临时文件 + replace；失败不留下半个 JSON 文件。

状态：concrete_implementation；语句执行：10/11。

### append_jsonl

```python
append_jsonl(path: Path, value: object) -> None
```

单写者追加一条 JSONL；不宣称支持多进程同时写或断电事务。

状态：concrete_implementation；语句执行：3/3。

### file_sha256

```python
file_sha256(path: Path) -> str
```

流式计算非敏感输入文件哈希；调用方不得将 .env 传入本函数。

状态：concrete_implementation；语句执行：5/5。

### source_fingerprint

```python
source_fingerprint(package_root: Path) -> str
```

以包内相对路径、Python 与可信前端源码计算指纹；不要求项目存在 .git。

状态：concrete_implementation；语句执行：6/6。

### prediction_row

```python
prediction_row(patch: PatchArtifact, model_label: str) -> dict[str, str]
```

补丁 → SWE-bench 三字段 JSONL；校验非空与内容哈希，不添加 resolved 字段。

正式字段依据官方 Evaluation Guide：instance_id、model_name_or_path、model_patch。
来源和访问日期见 docs/SOURCES.md；语法正确性由 ProgramAdapter 的实际 AST 后端额外校验。

状态：concrete_implementation；语句执行：4/4。

### CaseStore.emit

```python
CaseStore.emit(self, event: StageEvent) -> None
```

追加阶段状态到 trajectory/stages.jsonl；不包含模型凭证或原始 .env。

状态：concrete_implementation；语句执行：1/1。

### CaseStore.save

```python
CaseStore.save(self, name: str, artifact: StageArtifact) -> None
```

按类型写原始允许输入或中间产物；名称必须是单级安全文件名。

状态：concrete_implementation；语句执行：3/3。

### CaseStore.save_inference

```python
CaseStore.save_inference(self, record: object) -> None
```

记录生成状态与尚未评分的事实；不把 NOT_IMPLEMENTED 当作修复失败。

状态：concrete_implementation；语句执行：1/1。

### CaseStore.save_patch

```python
CaseStore.save_patch(self, patch: PatchArtifact) -> None
```

只保存最终补丁，不应用到用户仓库；x 模式防止覆盖同案例已有 patch。

状态：concrete_implementation；语句执行：3/3。

### BatchStore.create

```python
BatchStore.create(cls, results_root: Path, batch_id: str) -> 'BatchStore'
```

原子预留新 batch 目录；同名已存在立即失败，不隐式 resume 或覆盖。

状态：concrete_implementation；语句执行：6/6。

### BatchStore.create_case

```python
BatchStore.create_case(self, instance_id: str) -> CaseStore
```

创建五类案例子目录；instance_id 不能穿越到其他 batch 或用户项目。

状态：concrete_implementation；语句执行：5/5。

### BatchStore.record

```python
BatchStore.record(self, record: object, prediction: dict[str, str] | None=None) -> None
```

追加生成结果与可选预测；未实现/失败案例没有伪造的空 patch 预测。

状态：concrete_implementation；语句执行：3/3。

## src/boundary_repair/adapters/workspace.py

### validate_commit

```python
validate_commit(commit: str) -> str
```

Allow only full hexadecimal Git object ids; never options, branch names or revision expressions.

状态：concrete_implementation；语句执行：3/3。

### read_instance_manifest

```python
read_instance_manifest(path: Path | None, task: TaskInput) -> dict[str, Any]
```

Bind one task to an explicit repo/base/image entry, not a guessed Docker name.

状态：concrete_implementation；语句执行：10/13。

### extract_sources

```python
extract_sources(archive: Path, destination: Path, max_bytes: int) -> None
```

Extract only allowed regular production files; reject tar traversal and source symlinks.

Filtering is deliberate: dependencies, tests, history and secrets never enter generator
snapshots. Duplicate members and total extracted bytes are checked before each write.

状态：concrete_implementation；语句执行：29/34。

### GitArchiveWorkspaceAdapter.open_base

```python
GitArchiveWorkspaceAdapter.open_base(self, task: TaskInput, context: RunContext) -> Iterator[RepositorySnapshot]
```

Verify an exact commit and export a sanitized tree; never checkout or mutate the source repo.

状态：concrete_implementation；语句执行：23/29。

### DockerWorkspaceAdapter.open_base

```python
DockerWorkspaceAdapter.open_base(self, task: TaskInput, context: RunContext) -> Iterator[RepositorySnapshot]
```

A7: export the exact commit in a restricted container and remove the container on all paths.

No API keys, .env, raw dataset, baseline directory or Docker socket are mounted. The
analysis host parses text only; it never runs target source. Repository history remains
inaccessible to the model because only production files from git archive are extracted.

状态：concrete_implementation；语句执行：20/23。

## src/boundary_repair/algorithms/controls.py

### PlainControls.recover

```python
PlainControls.recover(self, task: TaskInput, snapshot: RepositorySnapshot, context: RunContext) -> ContractSet
```

C1: one ordinary evidence/caption request; all proposed requirements remain MAY.

状态：concrete_implementation；语句执行：6/6。

### PlainControls.locate

```python
PlainControls.locate(self, task: TaskInput, contracts: ContractSet, snapshot: RepositorySnapshot, context: RunContext) -> LocalizationResult
```

C2: same fixed candidate pool, original lexical order, no solver or negative certificates.

状态：concrete_implementation；语句执行：2/2。

### PlainControls.synthesize

```python
PlainControls.synthesize(self, task: TaskInput, contracts: ContractSet, localization: LocalizationResult, snapshot: RepositorySnapshot, context: RunContext) -> SynthesisResult
```

C3: generate once in the top available scope; do not use six-dimensional cost ordering.

状态：concrete_implementation；语句执行：11/12。

## src/boundary_repair/algorithms/expressivity.py

### ExpressivityLocalization.locate

```python
ExpressivityLocalization.locate(self, task: TaskInput, contracts: ContractSet, snapshot: RepositorySnapshot, context: RunContext) -> LocalizationResult
```

Assess a fixed candidate pool; no candidate patch or evaluator outcome is consumed.

状态：concrete_implementation；语句执行：7/7。

### ExpressivityLocalization.enumerate_boundaries

```python
ExpressivityLocalization.enumerate_boundaries(self, task: TaskInput, contracts: ContractSet, snapshot: RepositorySnapshot, context: RunContext) -> tuple[RepairBoundary, ...]
```

L1: enumerate read/modify interfaces; identical retrieval is reused by the plain control.

状态：concrete_implementation；语句执行：1/1。

### ExpressivityLocalization.build_local_model

```python
ExpressivityLocalization.build_local_model(self, boundary: RepairBoundary, contracts: ContractSet, snapshot: RepositorySnapshot, context: RunContext) -> LocalRepairModel
```

L2: pull MUST/frame requirements through the supported identity continuation.

Only matching, cited witnesses may constrain local outputs. MAY never produces a hard
exclusion. Missing witnesses, projections or mappings downgrade the model to PARTIAL.

状态：concrete_implementation；语句执行：20/24。

### ExpressivityLocalization.assess_expressivity

```python
ExpressivityLocalization.assess_expressivity(self, model: LocalRepairModel, contracts: ContractSet, context: RunContext) -> BoundaryAssessment
```

L3: require all four premises and complete declared scope before issuing a certificate.

状态：concrete_implementation；语句执行：13/14。

### ExpressivityLocalization.minimum_features

```python
ExpressivityLocalization.minimum_features(self, model: LocalRepairModel, context: RunContext) -> tuple[Feature, ...]
```

Find a smallest read subset by adding equal-output constraints for input collisions.

Unsupported input valuations leave the original interface intact. Each subset is solved
against all joint obligations, not just pairwise disjointness or lexical importance.

状态：concrete_implementation；语句执行：14/16。

### ExpressivityLocalization.rank_boundaries

```python
ExpressivityLocalization.rank_boundaries(self, assessments: tuple[BoundaryAssessment, ...], contracts: ContractSet, context: RunContext) -> LocalizationResult
```

L4: retain every assessment for auditing; only certified inexpressible interfaces are excluded downstream.

状态：concrete_implementation；语句执行：4/4。

## src/boundary_repair/algorithms/rendering.py

### HoleRenderer.render

```python
HoleRenderer.render(self, task: TaskInput, plan: PatchPlan, snapshot: RepositorySnapshot, context: RunContext) -> tuple[HoleFilling, ...]
```

Render untrusted syntax fragments, enforcing exact ids; the program adapter validates edits.

状态：concrete_implementation；语句执行：21/24。

## src/boundary_repair/algorithms/specification.py

### SpecificationRecovery.recover

```python
SpecificationRecovery.recover(self, task: TaskInput, snapshot: RepositorySnapshot, context: RunContext) -> ContractSet
```

Run S1-S4 exactly once without evaluator access, repair execution, or result feedback.

状态：concrete_implementation；语句执行：5/5。

### SpecificationRecovery.extract_evidence

```python
SpecificationRecovery.extract_evidence(self, task: TaskInput, snapshot: RepositorySnapshot, context: RunContext) -> EvidenceBundle
```

S1: send original evidence and base snippets; reject nonexistent quotes and invented sources.

状态：concrete_implementation；语句执行：5/5。

### SpecificationRecovery.bind_entities

```python
SpecificationRecovery.bind_entities(self, evidence: EvidenceBundle, snapshot: RepositorySnapshot, context: RunContext) -> tuple[EntityBinding, ...]
```

S2: retain all exact symbol bindings; lexical alternatives are explicitly PARTIAL.

Exact bindings exhaust only names in the indexed snapshot, not all possible visual-to-code
meanings. An index truncation propagates PARTIAL and cannot establish global coverage.

状态：concrete_implementation；语句执行：10/10。

### SpecificationRecovery.build_interpretation_space

```python
SpecificationRecovery.build_interpretation_space(self, evidence: EvidenceBundle, bindings: tuple[EntityBinding, ...], context: RunContext) -> InterpretationSpace
```

S3: construct separate acceptance variables and mutually exclusive interpretation choices.

Current observations never constrain desired output values. Alternatives are encoded,
not sampled. COMPLETE refers only to this finite acceptance theory; it is not a claim
that the model extracted all real-world interpretations or bound every repository entity.

状态：concrete_implementation；语句执行：10/11。

### SpecificationRecovery.derive_contracts

```python
SpecificationRecovery.derive_contracts(self, evidence: EvidenceBundle, bindings: tuple[EntityBinding, ...], space: InterpretationSpace, context: RunContext) -> ContractSet
```

S4: classify accepted requirements by entailment; uncertain frames remain MAY.

Witnesses contain cited input contexts only. Reachability remains UNKNOWN until a
supported code adapter establishes entry-local reachability. No LLM confidence can
promote it. MUST certifies acceptance under this theory, not correctness of perception.

状态：concrete_implementation；语句执行：21/24。

## src/boundary_repair/algorithms/synthesis.py

### boolean_cases

```python
boolean_cases(plan: PatchPlan) -> tuple[tuple[dict, bool], ...] | None
```

Convert explicit return-value obligations to finite examples; unsupported is not empty.

状态：concrete_implementation；语句执行：16/23。

### ScopeSynthesis.synthesize

```python
ScopeSynthesis.synthesize(self, task: TaskInput, contracts: ContractSet, localization: LocalizationResult, snapshot: RepositorySnapshot, context: RunContext) -> SynthesisResult
```

Run G1-G3 and syntax/hash materialization once; keep unresolved obligations in output.

状态：concrete_implementation；语句执行：6/6。

### ScopeSynthesis.enumerate_plans

```python
ScopeSynthesis.enumerate_plans(self, contracts: ContractSet, localization: LocalizationResult, snapshot: RepositorySnapshot, context: RunContext) -> tuple[PatchPlan, ...]
```

G1: produce bounded, fixed plans and count candidate/idea budgets before generation.

Primitive types constrain scope but do not pretend every consumer/alias transform has a
proved semantic adapter. UNKNOWN candidates remain usable, with explicit obligations.
Multiple samples are not adaptive retries; this version emits one selected filling.

状态：concrete_implementation；语句执行：20/23。

### ScopeSynthesis.select_minimal_scope

```python
ScopeSynthesis.select_minimal_scope(self, plans: tuple[PatchPlan, ...], contracts: ContractSet, snapshot: RepositorySnapshot, context: RunContext) -> PatchPlan
```

G2: rank unresolved requirements, frames, protected risks, extra scope, constants, size.

Analysis is conservative: an effect touching a protected property is a risk, not a proved
violation. No empty effect is interpreted as zero risk. No pre-fill obligation is claimed
discharged. Relevance order breaks exact cost ties without reading test outcomes.

状态：concrete_implementation；语句执行：17/18。

### ScopeSynthesis.fill_holes

```python
ScopeSynthesis.fill_holes(self, task: TaskInput, plan: PatchPlan, snapshot: RepositorySnapshot, context: RunContext) -> tuple[HoleFilling, ...]
```

G3: synthesize the supported Boolean grammar first; otherwise one JSON hole-filling call.

Missing samples, unsupported frames or an exhausted finite grammar do not create a
success. General code is produced once and remains semantically unproven. No error text
or evaluation result is fed back to a second model attempt.

状态：concrete_implementation；语句执行：8/8。

## src/boundary_repair/bootstrap.py

### build_pipeline

```python
build_pipeline(config: ExperimentConfig) -> RepairPipeline
```

按三个独立开关注入实现，生成全部八种消融组合；不读取密钥、不发网络请求。

未来接入只需替换适配器构造与相应实现，不需要修改 pipeline.py 的控制流。
有限布尔语义与单向通用补丁路径已实现；不支持的语义保持 UNKNOWN。

状态：concrete_implementation；语句执行：5/5。

### build_workspace

```python
build_workspace(config: ExperimentConfig) -> WorkspacePort
```

创建工作区适配器；支持 Docker 与显式本地 Git archive，bwrap 不静默回落到宿主执行。

状态：concrete_implementation；语句执行：2/6。

## src/boundary_repair/cli.py

### build_parser

```python
build_parser() -> argparse.ArgumentParser
```

定义五个显式命令；不提供会隐式循环修复/评分的 run-all 或自动重试参数。

状态：concrete_implementation；语句执行：16/16。

### main

```python
main(argv: list[str] | None=None) -> int
```

命令行 → 用例；0 为该命令完成，2 为输入/环境错误，3 为配置或兼容实现阻断。

plan/inspect 成功不代表 benchmark 成功。generate 的配置阻断返回 3，部分任务失败返回 4；不会伪造 patch 或评分。

状态：concrete_implementation；语句执行：7/25。

## src/boundary_repair/config.py

### _table

```python
_table(value: object, allowed: set[str], location: str) -> dict[str, object]
```

将 JSON 对象校验为已知字段映射；未知键立即报错，防止配置拼写静默失效。

状态：concrete_implementation；语句执行：6/7。

### _text

```python
_text(data: dict[str, object], key: str) -> str
```

取必需的非空文本；错误只输出字段名，不回显潜在敏感值。

状态：concrete_implementation；语句执行：4/4。

### _integer

```python
_integer(data: dict[str, object], key: str, minimum: int=1) -> int
```

取整数并检查下界；bool 不被当成整数预算接受。

状态：concrete_implementation；语句执行：4/4。

### _flag

```python
_flag(data: dict[str, object], key: str) -> bool
```

取显式布尔配置；字符串 false 不被按真值接受。

状态：concrete_implementation；语句执行：3/4。

### _path

```python
_path(value: str, root: Path) -> Path
```

解析本机路径；拒绝在 POSIX 上将 Windows 绝对路径误当相对目录。

状态：concrete_implementation；语句执行：3/4。

### load_config

```python
load_config(path: Path, project_root: Path | None=None) -> ExperimentConfig
```

JSON → 强类型配置；仅 project_root 相对配置文件，其余路径相对项目根。

不继承未知 baseline schema，不读取 .env，不自动查询 Git，不设置 safe.directory。
三个已有配置的预算语义已显式抄录到本包独立 preset；实际文件内容尚未核对。

状态：concrete_implementation；语句执行：26/35。

### parse_integration

```python
parse_integration(value: object, root: Path, target: str) -> IntegrationSettings
```

Validate optional deployment settings without reading secrets or initializing services.

状态：concrete_implementation；语句执行：25/33。

## src/boundary_repair/domain/errors.py

### ImplementationRequired.__init__

```python
ImplementationRequired.__init__(self, component: str) -> None
```

输入稳定的组件标识；保存标识用于结果落盘，不写入敏感上下文。

状态：concrete_implementation；语句执行：2/2。

## src/boundary_repair/domain/runtime.py

### BudgetLedger.remaining_seconds

```python
BudgetLedger.remaining_seconds(self) -> float
```

返回剩余墙钟秒数；外部请求/子进程必须使用不大于此值的超时。

状态：concrete_implementation；语句执行：1/1。

### BudgetLedger.check_deadline

```python
BudgetLedger.check_deadline(self) -> None
```

输入当前 ledger；超时即抛 BudgetExceeded，不重试或扩展预算。

状态：concrete_implementation；语句执行：2/2。

### BudgetLedger.begin_model_call

```python
BudgetLedger.begin_model_call(self, requested_output_tokens: int) -> int
```

调用前计数并返回可用输出上限；失败请求也计次，不允许隐式 SDK 重试。

后续适配器必须将返回值送给服务端 max-output 参数，并在返回后记实际 usage。
此函数不是后台硬超时器，也不能阻止不遵守接口的服务端超额生成。

状态：concrete_implementation；语句执行：7/8。

### BudgetLedger.record_output_tokens

```python
BudgetLedger.record_output_tokens(self, actual_tokens: int) -> None
```

累加服务端实际输出 token；超额立即终止，并保留实际消费而非截断数字。

状态：concrete_implementation；语句执行：4/5。

### BudgetLedger.claim_candidates

```python
BudgetLedger.claim_candidates(self, count: int=1) -> None
```

预留候选补丁数；方案枚举器调用，评分器不得借此触发新候选。

状态：concrete_implementation；语句执行：5/6。

### BudgetLedger.claim_ideas

```python
BudgetLedger.claim_ideas(self, count: int=1) -> None
```

预留不同修复方案数；同一题三个模块共享该计数。

状态：concrete_implementation；语句执行：5/6。

## src/boundary_repair/experiments/evaluation.py

### EvaluatorPort.evaluate

```python
EvaluatorPort.evaluate(self, request: EvaluationRequest) -> EvaluationReport
```

Evaluate immutable predictions; persist reports only, never regenerate candidates.

状态：interface_declaration；语句执行：0/0。

### validate_predictions

```python
validate_predictions(path: Path) -> tuple[dict[str, str], ...]
```

Accept the official three-string schema and unique safe ids; reject empty patches.

状态：concrete_implementation；语句执行：12/15。

### parse_case_reports

```python
parse_case_reports(root: Path, identifiers: tuple[str, ...]) -> dict[str, bool]
```

Read only per-instance report.json objects; process success is not considered a grade.

状态：concrete_implementation；语句执行：12/14。

### OfficialDockerEvaluator.evaluate

```python
OfficialDockerEvaluator.evaluate(self, request: EvaluationRequest) -> EvaluationReport
```

A8: validate artifact/version/image bindings, run harness once, then parse actual grades.

状态：concrete_implementation；语句执行：56/76。

### run_evaluation

```python
run_evaluation(config: ExperimentConfig, batch_directory: Path, evaluation_id: str, evaluator: EvaluatorPort) -> EvaluationReport
```

Require explicit server/Docker/version configuration and an existing generated batch.

状态：concrete_implementation；语句执行：9/13。

## src/boundary_repair/experiments/runner.py

### run_generation

```python
run_generation(config: ExperimentConfig, tasks: tuple[TaskInput, ...], batch_id: str, pipeline: RepairPipeline, workspace: WorkspacePort) -> BatchReport
```

任务序列/已注入依赖 → 新建批次、逐题产物和未评分报告。

每题同一 BudgetLedger；仅生成一个最终预测。未实现是工程阻断，记录后结束批次；
剩余题标为未尝试，不记成 unresolved。普通运行异常用类型名记录，不回显凭证。
不读取 .env；不评测、不回溯、不根据失败额外采样。调用方应先完成数据选择。

状态：concrete_implementation；语句执行：45/58。

## src/boundary_repair/kernel/boolean.py

### synthesize_boolean

```python
synthesize_boolean(names: tuple[str, ...], cases: tuple[tuple[dict[str, Scalar], bool], ...], context: RunContext, max_nodes: int=9) -> BooleanExpression | None
```

Enumerate typed !/&&/|| expressions by AST size, quotienting by behavior on all witnesses.

This is a finite, explicitly bounded grammar, not unrestricted program synthesis. Missing
reads, conflicting examples, and unsupported names are not silently replaced by constants.
Every returned expression is rechecked against all requirements and preservation examples.

状态：concrete_implementation；语句执行：33/38。

## src/boundary_repair/kernel/codec.py

### plain

```python
plain(value: object) -> Any
```

Convert immutable domain objects to JSON data, preserving all unresolved fields.

状态：concrete_implementation；语句执行：9/11。

### _unique_pairs

```python
_unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]
```

Reject duplicate JSON keys instead of silently accepting the last model value.

状态：concrete_implementation；语句执行：5/6。

### strict_json

```python
strict_json(text: str, maximum: int=2000000) -> dict[str, Any]
```

Require a single JSON object, bounded size, finite values and no Markdown extraction.

状态：concrete_implementation；语句执行：7/9。

### require_keys

```python
require_keys(data: object, required: set[str], optional: set[str] | None=None) -> dict[str, Any]
```

Validate exact object fields at an external boundary.

状态：concrete_implementation；语句执行：3/3。

### text_field

```python
text_field(value: object, maximum: int=200000) -> str
```

Return nonempty bounded text without coercing nonstrings.

状态：concrete_implementation；语句执行：2/3。

## src/boundary_repair/kernel/evidence.py

### parse_evidence

```python
parse_evidence(text: str, task: TaskInput, code_context: tuple[dict[str, object], ...]) -> EvidenceBundle
```

Verify source existence, unique ids, finite claim alternatives and typed observation keys.

状态：concrete_implementation；语句执行：45/72。

## src/boundary_repair/kernel/files.py

### allowed_source

```python
allowed_source(relative: str, for_edit: bool=False) -> bool
```

Filter dependencies, secrets, tests and generated outputs; edit policy is fail-closed.

状态：concrete_implementation；语句执行：8/11。

### safe_path

```python
safe_path(root: Path, relative: str) -> Path
```

Resolve only normalized relative paths with no symlinks anywhere in their ancestry.

状态：concrete_implementation；语句执行：9/12。

### source_slice

```python
source_slice(root: Path, span: SourceSpan) -> tuple[bytes, int, int]
```

Verify a byte-precise or legacy line span against the original content SHA256.

状态：concrete_implementation；语句执行：10/17。

### tree_digest

```python
tree_digest(root: Path) -> str
```

Hash every regular file in a sanitized snapshot; refuse links rather than following them.

状态：concrete_implementation；语句执行：8/9。

## src/boundary_repair/kernel/retrieval.py

### lexical_score

```python
lexical_score(query: str, text: str) -> int
```

Score distinct identifiers rather than repeated words; no gold-file hints are accepted.

状态：concrete_implementation；语句执行：4/4。

### snippets

```python
snippets(index: ProgramIndex, snapshot: RepositorySnapshot, query: str, limit: int=8, max_chars: int=100000) -> tuple[dict[str, object], ...]
```

Collect a stable top-k file context under a total character limit.

状态：concrete_implementation；语句执行：15/16。

### candidate_boundaries

```python
candidate_boundaries(task: TaskInput, index: ProgramIndex, snapshot: RepositorySnapshot, context: RunContext, maximum: int=40) -> tuple[RepairBoundary, ...]
```

Enumerate identical pools for research and plain localization, with explicit read interfaces.

Boolean-return interfaces include a constant-only edit and a parameter-reading edit. Other
AST nodes retain UNKNOWN semantics. File-level fallback remains available for unsupported
source languages, but its broad effect is not confused with a proven minimal repair.

状态：concrete_implementation；语句执行：18/18。

## src/boundary_repair/kernel/terms.py

### literal

```python
literal(value: Scalar) -> Term
```

Construct a scalar literal; validation occurs before solving/serialization.

状态：concrete_implementation；语句执行：1/1。

### symbol

```python
symbol(name: str) -> Term
```

Construct a named variable; symbols are Boolean unless a finite domain is declared.

状态：concrete_implementation；语句执行：1/1。

### term_from_json

```python
term_from_json(value: object, depth: int=0) -> Term
```

Parse the exact JSON AST schema, rejecting extra keys, deep trees and non-scalars.

状态：concrete_implementation；语句执行：14/19。

### term_json

```python
term_json(term: Term) -> dict[str, Any]
```

Serialize a term without provider-specific objects or executable text.

状态：concrete_implementation；语句执行：1/1。

### typed_key

```python
typed_key(value: Scalar) -> tuple[str, str]
```

Keep Boolean true distinct from integer 1 and missing information distinct from null.

状态：concrete_implementation；语句执行：1/1。

### walk

```python
walk(term: Term) -> tuple[Term, ...]
```

Return deterministic pre-order nodes; callers validate untrusted trees first.

状态：concrete_implementation；语句执行：1/1。

### symbols

```python
symbols(term: Term) -> tuple[str, ...]
```

Collect unique variable names in stable lexical order.

状态：concrete_implementation；语句执行：1/1。

### substitute

```python
substitute(term: Term, replacements: Mapping[str, Term]) -> Term
```

Capture-free substitution in a binder-free expression language.

状态：concrete_implementation；语句执行：3/3。

### _boolean

```python
_boolean(value: Scalar) -> bool
```

Reject Python truthiness as a substitute for a Boolean sort.

状态：concrete_implementation；语句执行：2/3。

### evaluate

```python
evaluate(term: Term, environment: Mapping[str, Scalar]) -> Scalar
```

Interpret supported finite expressions with strict sorts, never Python eval.

状态：concrete_implementation；语句执行：18/30。

### finite_domains

```python
finite_domains(assertions: tuple[Term, ...]) -> dict[str, tuple[Scalar, ...]]
```

Read only top-level domain declarations. Nested domains cannot restrict the universe.

状态：concrete_implementation；语句执行：13/17。

### literal_assignments

```python
literal_assignments(term: Term) -> dict[str, Scalar] | None
```

Extract a conjunction of symbol=literal assignments; None means unsupported, not a value.

状态：concrete_implementation；语句执行：13/17。

### boolean_environments

```python
boolean_environments(names: tuple[str, ...]) -> tuple[dict[str, Scalar], ...]
```

Enumerate the complete Boolean input domain, capped to keep local synthesis bounded.

状态：concrete_implementation；语句执行：3/3。

### infer_sort

```python
infer_sort(term: Term, variable_sorts: Mapping[str, type]) -> type
```

Statically validate all operator branches, including those in an inconsistent/empty domain.

状态：concrete_implementation；语句执行：12/22。

### declared_sorts

```python
declared_sorts(assertions: tuple[Term, ...], domains: Mapping[str, tuple[Scalar, ...]]) -> dict[str, type]
```

Preserve the declared sort even if intersecting finite domains becomes empty.

状态：concrete_implementation；语句执行：9/10。

### reduce_equalities

```python
reduce_equalities(assertions: tuple[Term, ...], domains: dict[str, tuple[Scalar, ...]]) -> dict[str, tuple[Scalar, ...]]
```

Soundly narrow domains using conjunctive equalities, never conditional/disjunctive facts.

状态：concrete_implementation；语句执行：29/32。

## src/boundary_repair/pipeline.py

### RepairPipeline.repair

```python
RepairPipeline.repair(self, task: TaskInput, snapshot: RepositorySnapshot, context: RunContext, trace: TracePort) -> SynthesisResult
```

顺序调用三个阶段，逐阶段保存中间产物；错误直接交给实验层分类。

输入必须已完成生成侧 allowlist 投影；输出仅表示 patch 已生成。
trajectory 记录公开阶段状态和产物引用，不索取模型私有推理链。

状态：concrete_implementation；语句执行：16/16。

## src/boundary_repair/ports.py

### ModelPort.complete

```python
ModelPort.complete(self, request: ModelRequest, context: RunContext) -> ModelResponse
```

输入原始证据请求；输出响应，调用前计次、调用后记 usage，禁止隐式重试。

状态：interface_declaration；语句执行：0/0。

### LogicPort.check

```python
LogicPort.check(self, assertions: tuple[Term, ...], context: RunContext) -> SolverAnswer
```

检查断言合取的可满足性并返回证书；必须验证算子、sort 和预算。

状态：interface_declaration；语句执行：0/0。

### ProgramPort.index

```python
ProgramPort.index(self, snapshot: RepositorySnapshot, context: RunContext) -> ProgramIndex
```

索引修复前符号与 AST 位置；不解析 node_modules、压缩副本或 Git 历史。

状态：interface_declaration；语句执行：0/0。

### ProgramPort.summarize

```python
ProgramPort.summarize(self, boundary: RepairBoundary, witnesses: tuple[Witness, ...], snapshot: RepositorySnapshot, context: RunContext) -> LocalRepairModel
```

构造读取接口、固定下游摘要和可达性前提；不支持的语法标为 PARTIAL。

状态：interface_declaration；语句执行：0/0。

### ProgramPort.effects

```python
ProgramPort.effects(self, plan: PatchPlan, snapshot: RepositorySnapshot, context: RunContext) -> tuple[Effect, ...]
```

估计实体-属性-上下文级影响与别名传播；可能影响不等于确定违反。

状态：interface_declaration；语句执行：0/0。

### ProgramPort.materialize

```python
ProgramPort.materialize(self, task: TaskInput, plan: PatchPlan, fillings: tuple[HoleFilling, ...], snapshot: RepositorySnapshot, context: RunContext) -> PatchArtifact
```

核验源码哈希后在隔离副本应用 AST 编辑、生成唯一 diff；不运行评分测试。

状态：interface_declaration；语句执行：0/0。

### WorkspacePort.open_base

```python
WorkspacePort.open_base(self, task: TaskInput, context: RunContext) -> AbstractContextManager[RepositorySnapshot]
```

创建并最终清理隔离工作区；固定 commit/镜像，不能信任未校验的仓库状态。

状态：interface_declaration；语句执行：0/0。

### SpecificationPort.recover

```python
SpecificationPort.recover(self, task: TaskInput, snapshot: RepositorySnapshot, context: RunContext) -> ContractSet
```

issue/附件/base code → 带来源的 MUST、MAY、FRAME 与见证。

状态：interface_declaration；语句执行：0/0。

### LocalizationPort.locate

```python
LocalizationPort.locate(self, task: TaskInput, contracts: ContractSet, snapshot: RepositorySnapshot, context: RunContext) -> LocalizationResult
```

规格/base code → 可表达、不可表达与未知的修复接口集合。

状态：interface_declaration；语句执行：0/0。

### SynthesisPort.synthesize

```python
SynthesisPort.synthesize(self, task: TaskInput, contracts: ContractSet, localization: LocalizationResult, snapshot: RepositorySnapshot, context: RunContext) -> SynthesisResult
```

行为义务/候选接口 → 一个作用域计划与最终补丁；不能消费评分结果。

状态：interface_declaration；语句执行：0/0。

### TracePort.emit

```python
TracePort.emit(self, event: StageEvent) -> None
```

写入阶段名与状态，不自动保存模型私有推理或敏感环境。

状态：interface_declaration；语句执行：0/0。

### TracePort.save

```python
TracePort.save(self, name: str, artifact: StageArtifact) -> None
```

以固定文件名持久化类型化中间产物；路径限制由存储适配器负责。

状态：interface_declaration；语句执行：0/0。
# 三层机制协议补充

当前生成证据使用 `evidence.v6`，普通事务仍为 `edits.v5`。新增程序情境目录、绑定备选、部分义务编译检查，以及不清空其他模块输入的消融交接，见 [MECHANISM_ACTIVATION.md](MECHANISM_ACTIVATION.md)。历史证据读取继续使用对应旧解析入口。
