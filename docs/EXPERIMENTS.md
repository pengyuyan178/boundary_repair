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
本版不自适应切换参数，也不自动网络重试；不兼容该 Chat Completions 协议的服务会明确失败。

## 3. Node/TypeScript

在 code/boundary_repair 执行 `npm ci --ignore-scripts --no-audit --no-fund`，固定 5.8.3。
也可以明确指定可信已安装 package 路径。node_candidates 可填可执行文件或其目录；随后回退 PATH。
给出的两个 Node 候选路径仍保留，但从未声明已访问或可用。

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
不要直接开始全量：正式 API/Docker/镜像/harness 在本交付环境没有联调。

## 6. 参数含义

seed42、3600秒、100次模型调用、150000生成tokens、20候选、5思路、温度1.0保留。
这些是上限，不是每题必须使用的数量。v0.2 选择一个计划后只填充一次；不运行后反馈、不多轮调试。
prefer_greedy_patch=true 时正常优先确定性布尔合成；当前无独立非greedy搜索策略，false 不启用新算法。
allow_multiple_sampling 只表示许可，本版没有另行多采样策略，不能将其宣称已完整实现。
require_witness 在可表达性路径要求见证；缺失时 UNKNOWN，普通生成仍可以执行。

## 7. 结果与评分

默认 result/method/boundary_repair/<batch>，baseline 目录原样保留。
原始 dataset/方法源码/预测有哈希；模型实际响应元数据对象有 provider ID/usage，但本版未记录完整原始 HTTP 请求历史。
不是完整可重放在线 API 账本。不得称 fixture 为真实模型、generated 为 resolved。

官方预测三字段为 instance_id/model_name_or_path/model_patch。
run_id 包含 evaluation_id 和预测哈希，避免不同补丁复用旧缓存。
读逐题 report.json 的真实布尔 resolved；selected/submitted/graded/infrastructure_errors 分开。
生成未成功的题目仍在 selected 分母，不从总解决率中删除。

## 8. 实际验证层级

自测：有限域、证据、AST、真实 Git/Node/HTTP loopback、八种模块组合。
模拟验证：TLS 下载响应、Docker 参数/清理、官方 harness 版本/报告契约。
未验证：Windows OS 分支、用户 SSH、真实供应商、公共图像感知、Docker daemon、官方真实任务成绩。
