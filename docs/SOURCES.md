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

npm 在线安装未成功验证；本轮实际解析使用现有 TypeScript 5.8.3。
接口文档核验不等于在用户提供的服务器上运行成功。
