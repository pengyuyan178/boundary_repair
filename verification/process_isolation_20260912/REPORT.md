# 生成与评测进程隔离修复

日期：2026-09-12。沿用用户在上一会话确定的方案：产出 patch 的进程与评测进程分开，只有评测侧读取原始答案。
基于已通过335项Docker测试的代码检查点 `9b68fb0264fd99d5cd99dd73458b682dd7126c13`；未修改三层算法。

## 实现

- 恢复上一会话中断时删除的 `scripts/run_layers.py`，补齐独立的 `prepare`、`generate`、`evaluate` 入口。
- `prepare` 为评测侧的数据导出步骤：固定 seed 42，完整选择dev 100题或test 480题，只输出任务白名单及issue附件。
- `generate` 的宿主调度器不打开原始数据集。实际模型调用及三层算法只在逐题的非root Docker容器执行。
- 容器仅挂载方法源码、入口、TypeScript、该题净化输入、base_commit导出的生产源码，以及该题自己的输出目录。
- 前五项只读；容器根文件系统只读，去除Linux capabilities，禁用提权，使用独立进程命名空间，不挂载Docker socket、宿主项目或评分目录。
- 仅向生成容器传入配置指定的模型环境变量，不挂载整个 `.env`。随机种子和 `PYTHONHASHSEED` 固定为42。
- 容器结束后才收集产物。全部生成完成后冻结预测、结果、批次清单及镜像清单；评测另行启动，先验证冻结产物，再读取原始数据。
- 服务器上的旧 `run.py generate` 路径拒绝执行；`run_generation` 同样校验隔离工作进程入口。
- 失败工作进程的输出和日志保存在批次目录，容器清理不会删除它们。

文件访问隔离针对实际执行模型和算法的生成容器。准备与评分程序属于可信评测侧；宿主调度器仍有Docker管理权限，本实现没有更改服务器管理员的文件权限。

## 评分完整性

评测原始数据使用独立的 `evaluation_dataset_sha256`，不再错误地与净化任务文件哈希比较。
评分要求真实测试日志包含开始、结束标记及唯一退出码；报告成功时，退出码必须为0。
Chart.js还要求Chrome和Firefox分别完成全部测试，且没有断连。完整运行而测试失败仍可计为未修复。
不完整或矛盾的日志记为基础设施问题，保留原始官方报告并另写 `execution_integrity.json`。

浏览器检查直接复用服务器已有 `TraceRepair/releases/isolation_20260907/scripts/evaluate_chartjs_case.py::browser_audit`
的完成进度判定，并补上大小写无关的断连识别和零测试拒绝。容器参数依据[Docker运行文档](https://docs.docker.com/engine/containers/run/)
及[bind mount文档](https://docs.docker.com/engine/storage/bind-mounts/)。

## 验证记录

所有动态检查在4090服务器执行。服务器验证目录：
`/home/ubuntu/anaconda3/envs/pyy/paper/newGUIRepair/result/verification/process_isolation_20260912_resume/`。

| 检查 | 结果 |
|---|---|
| 完整回归测试 | 350项全部通过，162.905秒，见 `tests.log` |
| 最终入口相关回归 | 12项全部通过，见 `final_isolation_tests.log` |
| 净化dev输入 | 完整100题，任务与附件严格白名单通过 |
| 净化test输入 | 完整480题，任务与附件严格白名单通过 |
| 实际隔离探测 | `markedjs__marked-2627`，容器退出码0 |
| 实际只读源码解析 | 同题census容器退出码0，不调用模型 |
| 原始dev/test答案文件的实际open调用 | 均失败 |
| 经 `/proc/1/root` 访问宿主答案的实际open调用 | 失败 |
| Docker socket及原镜像Git目录的实际open调用 | 均失败 |
| 只读base源码 | 通过 |
| 已归档官方评测日志复核 | 47份；6份Chart.js报告被识别为不完整 |

两份原报成功案例保留完整执行记录：`Automattic__wp-calypso-33752`、`processing__p5.js-3680`。
本轮没有重新生成补丁、没有运行正式测试评分、没有覆盖旧批次汇总，不发布新的修复率。
`runtime_verification.json`、`archived_grading_integrity.json`记录实际权限与逐题日志检查；这些均为工程验证，模型调用数为0。

生成镜像ID：`sha256:cca8ab072ceecfb28d3ebcbe207eec942deb1ca563225f1652b36184f26aa53d`。
Python基础镜像通过已有DaoCloud入口下载，digest为
`sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84`。
镜像源连接失败与误用评分解释器导致的早期验证日志保存在服务器 `old_data/`，成功记录单独保存。
宿主准备/调度程序使用原实验的 `TraceRepair/releases/isolation_20260907/.venv/bin/python`；
官方评测仍使用配置中的 `anaconda3/envs/swebench/bin/python`。

## 自查

修改没有使用gold内容来选择生成位置、改动算法或重新采样；净化过程只按固定字段投影。
评分检查对已解决和未解决报告一致应用，不为了增加或减少修复数调整规则。
完整测试失败与测试未完成保持区分；这次工程验证不证明三层方法优于baseline。
旧实验及其他未提交改动保留；本次代码基于原验证检查点写入独立Git分支。
