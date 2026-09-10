# BoundaryRepair 0.2.0：实现与验证报告

日期：2026-09-09。交付范围：可运行的有限语义实现，不是已获真实 benchmark 成绩的系统。

## 已实现

上一版 23 个待实现入口均有函数体，并均被本次测试执行到。现在生产源码中有 129 个显式函数/方法定义，
其中 13 个为 Protocol 接口，116 个为具体实现；116 个具体函数体均有执行覆盖。
这不表示每个分支都有覆盖，也不表示每个函数的所有输入已被证明正确。

逻辑内核、来源/实体/解释规格、可表达性、最小特征、作用域计划、布尔合成、受限模型填充、
TypeScript AST、Git archive、模型 HTTP、结果存储和独立评分适配器均有实际代码。

## 实际自检

| 项目 | 结果 |
|---|---|
| unittest | 115 通过；0 失败；0 错误；0 跳过 |
| 生产 Python 语句覆盖 | 2182/2493，87.53% |
| 分支覆盖 | 607/864，70.25% |
| coverage.py 行与分支合计指标 | 83.08% |
| Python 编译、3.11 语法解析 | 通过；实际执行环境为 3.13.5 |
| 离线 wheel 构建 | 通过 |
| ZIP 解压到中文路径后重测 | 115 通过 |
| 解压后独立 smoke | 通过 |

环境：Linux、Python 3.13.5、Node 22.16.0、TypeScript 5.8.3、Git 2.47.3。
并没有在用户 Windows/SSH 上执行。

## 整条链真实执行了什么

隔离生成一个带错误的 JavaScript 仓库，读取其 exact Git commit，解析真实源码，
读取明确标记的结构化证据 fixture，恢复约束，排除无法区分行为的常量接口，
选择参数接口，以有限语法合成器计算新表达式，生成 unified diff。
随后由独立测试复制原仓库、执行 git apply --check 和 git apply、运行 Node 检查结果。

四个输入情境的原输出为 `[true, true, false, true]`，补丁后为 `[true, false, false, false]`，
符合合成 issue 的要求。证据 fixture 不含最终 patch，补丁由算法计算，原仓库保持不变。
模型调用计数 1 指一次 fixture 读取，不是真实 API 推理调用；不是 SWE-bench 解题成功。

## 关键回归

MAY 不升级为硬约束；布尔值不和整数 1 混淆；截断/不支持理论不返回 UNSAT；
矛盾解释不产生空集蕴含；缺少证明前提不剪枝；Unicode 按 UTF-8 字节修改；
源码漂移/重叠/越界/no-op 拒绝；无末尾换行 diff 实测；模型失败不伪造输出；
生成与评分状态分离；fixture 批次禁止正式评分。

## 没有完成或验证的部分

真实模型及公共图像视觉抽取：没有调用。HTTP 测试为真实本地连接到模拟服务；TLS 图像测试为模拟。
Docker 和官方 harness：代码已接入，但本次只做契约模拟测试，没有 Docker daemon。
复杂渲染/消费通路/跨函数别名/身份绑定：尚未实现完整专用语义；输出 UNKNOWN，普通单次模型生成仍未证明。
MUST 相对于抽取后的有限理论，不证明视觉/文字解释本身正确。
纯布尔函数的入口可达性不等于 UI 到内部函数的应用级可达性。
没有真实任务修复率、无 baseline 对比、无创新度或性能保证。

## 文件导航

`verification/placeholder_completion.md`：原 23 个入口逐项核对。
`verification/function_audit.json`：所有函数、范围、未覆盖语句和状态。
`verification/tests.log`、`coverage.json`、`coverage.txt`：原始测试证据。
`verification/smoke_generated.patch` 与 smoke_*：实际生成补丁及各阶段产物。
`docs/API_REFERENCE.md`：从源码生成的函数签名与注释。
`docs/IMPLEMENTATION.md`：算法实现及边界。
`docs/EXPERIMENTS.md`：接入用户 API/Docker/harness 的配置契约。
