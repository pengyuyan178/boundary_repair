# 外部接口核验（2026-09-09）

这里只核验公共工具接口，不将引用当作自有算法创新证据。没有复制其他论文的修复系统实现。
依赖 TypeScript 编译器；它是外部基础设施，遵守其 Apache-2.0 许可，不打包其 node_modules。

| 来源 | 用于核验 |
|---|---|
| https://github.com/microsoft/TypeScript/wiki/Using-the-Compiler-API | createSourceFile/forEachChild、parse-only AST |
| https://git-scm.com/docs/git-archive | 从指定 commit 导出树，不 checkout |
| https://platform.openai.com/docs/api-reference/chat/create | Chat Completions、usage/max_completion_tokens/JSON 输出接口 |
| https://platform.openai.com/docs/guides/structured-outputs | JSON mode 不保证业务 schema；本地仍须校验 |
| https://www.swebench.com/SWE-bench/guides/evaluation/ | 预测三字段、逐题报告、run_id+instance_id 缓存 |
| https://github.com/SWE-bench/SWE-bench | 当前 v5 CLI 与保留的 legacy module CLI |
| https://docs.docker.com/engine/containers/run/ | network/read-only/capabilities/资源选项 |

npm 在线安装未成功验证；初次实现实际解析使用现有 TypeScript 5.8.3。
接口文档核验不等于在用户提供的服务器上运行成功。

## edits.v5 精确替换（2026-09-12）

- 复用本项目 `transaction_contents` 已有的唯一原文精确匹配实现，将目标解析扩展到冻结的语法块；保留原编码、哈希和事务检查。
- SEARCH/REPLACE 表达方式参照 [Aider 编辑格式](https://aider.chat/docs/more/edit-formats.html)。本项目使用结构化 JSON 和预授权目标 ID，不引入 Aider 的整文件权限或宽松匹配逻辑。
- 属于通用编辑工程，研究方法与受控消融共用，不声明为论文算法创新。

## edits.v4 编辑边界（2026-09-11）

直接复用现有 TypeScript 5.8.3 Compiler API 的 `createSourceFile`、`forEachChild`、`getStart/end` 和 `parseDiagnostics`。没有引入新解析依赖，也没有复制 Aider、SWE-agent 或 ast-grep 的修复实现。程序绑定语法范围和唯一原文匹配属于公共编辑基础设施，不作为三个研究模块之外的新算法创新。

| 参考 | 实际采用与未采用 |
|---|---|
| https://developers.openai.com/cookbook/examples/gpt4-1_prompting_guide | 采用避免让模型计算编辑行号的接口原则；未复制其补丁执行器 |
| https://aider.chat/docs/more/edit-formats.html | 参考原文定位方式；实现仅在冻结窗口中精确唯一匹配，不进行模糊补救 |
| https://ast-grep.github.io/guide/rewrite-code | 参考解析器拥有节点边界的原则；实际使用已有 TypeScript，而不是新增 ast-grep 依赖 |
| https://arxiv.org/html/2405.15793v3 | 了解编辑错误反馈机制；未采用重试，因为当前协议要求一次性生成 |

新测试只使用显式合成源码；没有将历史失败响应、gold patch 或官方测试输出作为生成输入。工程验收记录独立保存在 `verification/20260911_edit_boundaries/`。
