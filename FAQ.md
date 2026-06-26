# FAQ

## Athena Agent 是否依赖第三方 Agent 框架？

不依赖。项目依赖 LiteLLM 做模型适配，但 Agent 执行循环、工具注册、记忆系统、自进化和可观测性由项目直接实现。

## 没有 API Key 能演示吗？

可以。6 个面试 demo 默认使用 deterministic runner 或 mock 数据，不依赖真实 LLM、云账号或 K8s 集群。

## 性能数据是否真实？

当前只保留可复现命令和待补录表，不写无法验证的数字。核心覆盖率和测试结果可以通过本地命令复现。

## 为什么覆盖率报告采用核心口径？

CLI/TUI、外部集成适配器、观测 Web 页面和文件/Git/代码解析工具属于外部边界或展示层，当前先对核心 Agent、Memory、Learning、API、CloudOps 做 80% 以上覆盖率约束。全仓覆盖率后续需要继续补适配层测试。

## 面试时先演示哪个 demo？

推荐顺序是 demo1、demo2、demo4、demo6。它们能快速覆盖开发者助手、自进化、云排障和告警闭环。

## 如何接真实云环境？

从 CloudOps client 边界替换 mock client，先接只读接口，再逐步开放写操作。写操作必须保留 confirmed 和审计日志。