# 授权范围内的 SEARCH/REPLACE：实现与工程验证

日期：2026-09-12。事务协议更新为 `edits.v5`；基于进程隔离检查点
`f469b7f11b87b437e4051d7648c6658cef4c477d`。已实现并通过4090 Docker的360项回归，零失败、零跳过。
依用户后续要求，未启动dev全量或小批次实验，模型调用数为0，不报告新的修复率。

## 实现

- 复用 `transaction_contents` 原有精确匹配实现，`replace_text.target` 可绑定冻结的语法块ID或text窗口ID。
- 搜索只在目标原文内进行；必须唯一、逐字符匹配。syntax窗口ID、已剔除块、窗口外原文均不授予编辑权限。
- 同一块可含多个不重叠搜索；各编辑都基于冻结原文，禁止依赖前一个编辑产生的文本。
- 不扩大到父块、整文件或其他文件，不模糊匹配；保留原编码、BOM、换行风格、源码哈希、原子事务和实际Git应用检查。
- 精确替换不自动补回被old_text消耗的末尾换行；原 `replace_block` 的末尾换行兼容行为保留。
- 模型首选SEARCH/REPLACE；提示明确new_text只替换old_text。语法块附原文哈希、字符数、首尾各至多160字符的原样片段及阅读位置，避免重复发送完整嵌套块。
- 保留旧块替换、插入和已有文件操作；局部表达式等受限合成路径沿用原来的语法限制。证据和三层算法未修改。
- `edits.v5` 必须提供输出Schema。匹配、越界、重叠失败保留各自component，并记application_check=failed；语法与模型服务错误仍分别记录。
- 仅一次模型生成，不回传语法错误、不重试、不自动删逗号或补括号。研究方法与受控消融共用该编辑层，第三方baseline未修改。

SEARCH/REPLACE格式参考[Aider官方编辑格式说明](https://aider.chat/docs/more/edit-formats.html)；实现复用项目已有编译器，属于通用编辑工程，不作为论文创新。

## 历史原因核验

读取旧批次的请求、原始响应、冻结计划与语法拒绝记录，对所选块逐一验证原文哈希。
历史源码检查点为 `9b68fb0264fd99d5cd99dd73458b682dd7126c13`，请求协议均为 `edits.v4`。

| 案例 | 原目标与错误输出 | 已确认的目录事实 |
|---|---|---|
| marked-1435 | B023错误信息拼接语句被替换为导出语句与外层闭包结束代码 | 目录同时包含marked函数B014和try语句B020 |
| react-pdf-433 | B067解构声明被替换为完整drawBorders方法 | 完整drawBorders方法B065已在授权目录 |
| Chart.js-8710 | B002 numeric方法的输出多了成员逗号 | 原块在闭括号结束，逗号位于块外；父对象B000也已授权 |
| marked-2483 | B001 return语句输出多了外层函数结束括号 | 完整getDefaults函数B000已在目录 |
| wp-calypso-26286 | B081 DOM引用赋值被替换为完整isVisible方法 | 包含目标的AppBanner类B080已在目录 |
| wp-calypso-21648 | 非法default function、无关Placeholder和固定示例标题 | 另有定位及需求理解错误，不能靠替换格式确认解决 |

这些记录不能支持“全部因为只给了过小槽位”的归因：前5题的目录均存在更大的授权块。
它们体现模型选错目标、混淆目标语法角色或输出越过分隔符边界。新协议要求显式原文匹配并增加边界提示，
但尚无新模型实验能证明边界错误发生率下降。SEARCH/REPLACE也不能保证选对组件或生成正确逻辑。

## 验证证据

服务器目录：`/home/ubuntu/anaconda3/envs/pyy/paper/newGUIRepair/result/verification/search_replace_20260912/`。

| 检查 | 结果 |
|---|---|
| 完整Docker回归 | 360项全部通过，175.488秒；见tests.log与tests_status.json |
| 本次新增回归 | 10项：范围、重复与精确匹配、多编辑、原子冲突、编码与换行、Schema、单次生成和非法语法 |
| 历史旧输出复现 | 6/6正确拒绝新增语法错误 |
| 相同旧输出改为SEARCH/REPLACE编码 | 6/6仍拒绝语法错误，未放松检查 |
| 输入源码完整性 | 原始文件和所选块哈希匹配，复现后原源码未改动 |
| 模型调用/新benchmark运行 | 0 / 0 |

测试容器使用固定镜像 `sha256:cca8ab072ceecfb28d3ebcbe207eec942deb1ca563225f1652b36184f26aa53d`，
非root、只读方法代码、无网络、PYTHONHASHSEED=42，复用已有TypeScript 5.8.3。测试容器不挂载原始数据集、密钥或Docker socket。
历史复现只挂载已筛选的修复前源码、编辑权限与旧响应；没有gold/test patch或官方测试反馈。
上述复现没有生成纠正后的补丁，不能把12次正确拒绝统计为修复成功。

第一次完整测试为359通过、1失败，原因是旧断言仍期待unknown_edit_region，现协议统一使用unknown_search_target；
更新断言后完整回归通过。首次日志及源码包归档于服务器old_data/initial/。
原marked-1435镜像已删除，最终从现有 `/data/zy/tracerepair_base_inputs/` Git缓存读取指定base_commit的文件并校验哈希。
曾计划从GitHub补取原文件，但被自动审批拒绝，网络动作未执行；最终复现完全使用本地来源。

## 交付与自查

- 本地当前工作文件已更新；运行代码、测试和文档见changes.json与changes.patch。
- 服务器交付使用上述验证目录的method/快照。服务器主code/boundary_repair/仍为旧版本，与本次基线不匹配，未覆盖。
- 后续批次应从此method/或本次Git检查点冻结新的method_snapshot，不混用旧批次代码和结果。
- 新旧数据分目录归档，历史实验统计未改写；Git提交基于已核验完全一致的f469b7f工作前快照，既有分支和暂存区保留。
- 没有按实例ID修改生产算法，没有读取答案决定位置，没有为了增加产出数放松语法检查或自动重采样。
- 尚待其他工作完成后统一验证真实模型边界错误率、语义修复效果及baseline差异。
