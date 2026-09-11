# 环境接入与实验口径

## 1. 本地与服务器

Windows 仅用于开发/检查；正式配置 target=server 强制 Docker+HTTP，不允许 fixture 或 git_archive。
项目根无需 Git。`baseline/*` 不是待修复目标代码库，不应作为 repository_manifest 中的 repo 路径。

独立本地 Git 调试使用 integration.workspace_mode=git_archive 和 repository_manifest。
读取 exact base_commit 的 archive，不改分支/工作树、不读取 .git 历史到模型、不设置全局 safe.directory。
目录所有者不同时，仅在确认该具体目标仓库可信后设置该条目的 trust_directory=true。

## 2. 模型

model.name_env/endpoint_env/api_key_env 是已有 code/.env 的键名，不是值。
默认 MODEL_NAME/MODEL_BASE_URL/MODEL_API_KEY；按实际已有键修改配置。支持有引号/注释的简单赋值，不执行扩展。
调用时才读取，环境变量覆盖文件；结果目录不保存密钥。HTTP JSON 协议要求 completion_tokens、finish_reason=stop。
本版不自适应切换参数，也不自动重试模型请求。默认发送 `response_format.type=json_schema`、
`strict=true` 和真实递归 Schema。仅显式配置 `integration.response_format=json_object` 才使用 JSON mode，
两者执行同样的本地 v2 来源、逻辑形状、候选组和编辑检查。不能依据某题结果自动切换。
401/403/404 和明确指向 Schema 的 400/422 作为配置阻断；上下文超限等单题错误不据此阻断整个批次。
服务端未说明原因的拒绝保留为外部服务错误，不猜测支持能力。

`integration.asset_attempts` 默认为 3，最大 5；`asset_retry_delay` 默认 1 秒，按指数退避。
只重试图片传输中的暂时超时、连接中断、临时 DNS 和指定暂态 HTTP 状态，计入原墙钟预算。
证书、私网目的地、MIME/大小错误、重定向和普通 4xx 不重试。
工作区就绪后独立预取图片；后续模型调用复用逐题缓存，不重复下载。
缓存损坏明确失败，不静默换图；日志保留错误类别而不是可能含敏感信息的底层消息。

## 3. Node/TypeScript

在 code/boundary_repair 执行 `npm ci --ignore-scripts --no-audit --no-fund`，固定 5.8.3。
也可以明确指定可信已安装 package 路径。node_candidates 可填可执行文件或其目录；随后回退 PATH。
每次冻结都须核实这些路径；候选路径本身不是可用性的证明。

## 4. 正式镜像/评分配置

复制 configs/image_manifest.example.json 到你自己的配置文件，逐实例填入：repo、完整 base_commit、
image@sha256、容器源码位置、官方 harness 实际使用的镜像 alias。
不得根据名字猜测 digest，不会自动 pull、retag 或更改已有 baseline 镜像。

generation 验证 task/repo/base 并用 digest 指定镜像导出 exact commit。
evaluation 验证所声明 alias 与 digest 的本地 image ID 相同；该机制不是对实际 harness 全部启动命令的实时拦截。
首次服务器运行仍须检查 harness 日志与镜像选择，尤其版本变更后。

配置 image_manifest 为上述文件路径；harness_python 必须是能 import swebench 的 Python 可执行文件，
不能将给出的 `/home/ubuntu/anaconda3/envs/pyy/paper` 目录自动当作解释器。
harness_revision 显式使用 `version:<确切版本>` 或 `git:<40位commit>`；适配器检查版本及 --help 能力。

## 5. 命令

服务器上先根据上述要求填写配置，然后：

```bash
cd /home/ubuntu/anaconda3/envs/pyy/paper/newGUIRepair/code/boundary_repair
python run.py doctor --config configs/server_docker.json
python run.py inspect --config configs/chartjs_server.json --repo chartjs/Chart.js
python run.py generate --config configs/chartjs_server.json --repo chartjs/Chart.js --limit 1 --batch chartjs-smoke-001
python run.py evaluate --config configs/chartjs_server.json \
  --batch-directory /home/ubuntu/anaconda3/envs/pyy/paper/newGUIRepair/result/method/boundary_repair/chartjs-smoke-001 \
  --evaluation-id chartjs-grade-001
```

这里的 python 指已经激活并验证的服务器解释器，不预设未知可执行路径。
旧随机 10 题批次已联调真实 API/Docker/harness，结果为 0/10。v2 不能继承旧版的供应商兼容性结论；
工程回归、供应商协议验证与正式任务评测应分开记录。

## 6. 参数含义

seed42、3600秒、100次模型调用、150000生成tokens、20候选、5思路、温度1.0保留。
这些是上限，不是每题必须使用的数量。v0.2 选择一个计划后只填充一次；不运行后反馈、不多轮调试。
prefer_greedy_patch=true 时正常优先确定性布尔合成；当前无独立非greedy搜索策略，false 不启用新算法。
allow_multiple_sampling 只表示许可，本版没有另行多采样策略，不能将其宣称已完整实现。
require_witness 在可表达性路径要求见证；缺失时 UNKNOWN，普通生成仍可以执行。

## 7. 结果与评分

默认 result/method/boundary_repair/<batch>，baseline 目录原样保留。
原始 dataset/方法源码/预测有哈希；每个模型请求保存 prompt、Schema、参数、附件 ID/hash，
响应保存文本、实际模型名、provider ID/usage 与响应字节哈希。认证头与图片 base64 不进入请求日志。
这不是可以重现远端模型随机性的完整账本。不得称 fixture 为真实模型、generated 为 resolved。

官方预测三字段为 instance_id/model_name_or_path/model_patch。
run_id 包含 evaluation_id 和预测哈希，避免不同补丁复用旧缓存。
读逐题 report.json 的真实布尔 resolved；selected/submitted/graded/infrastructure_errors 分开。
生成未成功的题目仍在 selected 分母，不从总解决率中删除。

## 8. 实际验证层级

自测：有限域、证据、AST、真实 Git/Node/HTTP loopback、八种模块组合；新记录在
`verification/20260910_local_edits/`，旧交付的验证文件保留但不能当作当前覆盖率。
模拟验证：TLS 图片传输及重试、Docker 参数/清理、官方 harness 版本/报告契约。
真实服务和正式成绩需按具体版本、批次分别记载；尤其不能把 v2 工程测试当作真实模型修复成功。

## 9. 冻结与比较

证据协议、检索、图片缓存/重试、局部编辑与结构预算必须同步用于研究版本和普通对照。
改变 response_format 或其他协议配置要生成新批次并记录，不允许在某题失败后偷偷切换。
固定原 seed、模型、候选/调用/token/时间上限；不读取 gold/test patch/修复后图片来决定位置或生成内容。
同组开发回归只能用于开发诊断，不作为独立泛化证据；正式比较须冻结版本后使用独立选择的完整评测集合。
不执行多轮模型纠错或官方测试反馈，UNKNOWN/无补丁/附件失败的题目仍保留在 selected 分母。
