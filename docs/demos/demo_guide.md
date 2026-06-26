# Athena Agent Demo 指南

## Demo 1：代码智能助手

命令：`python examples/demo1_code_analysis.py`

讲解重点：Tree-sitter 解析代码结构，Tool Registry 统一调用工具，最后生成单元测试草案。该 demo 不依赖 LLM，适合面试开场展示项目的开发者助手能力。

## Demo 2：自进化演示

命令：`python examples/demo2_self_evolution.py`

讲解重点：一次复杂任务的轨迹被 ComplexityEvaluator 评分，SkillGenerator 生成可复用 Skill，SkillLibrary 在下次相似 query 中召回。

## Demo 3：调试面板展示

命令：`python examples/demo3_debugger.py`

讲解重点：Tracer 保存完整执行轨迹，StepDebugger 支持断点和暂停，Metrics/Token 统计用于 Web Console 展示。

## Demo 4：K8s 故障自动排查

命令：`python examples/demo4_k8s_diagnose.py`

讲解重点：模拟 CrashLoopBackOff、ImagePullBackOff 和资源压力，通过 K8sDiagnoser 输出根因和建议。

## Demo 5：云成本智能优化

命令：`python examples/demo5_cost_optimize.py`

讲解重点：复用 AthenaWebService 的 CloudOps cost 模式，扫描低 CPU 实例并估算月度节省。

## Demo 6：告警自动处置

命令：`python examples/demo6_alert_auto_handle.py`

讲解重点：Alertmanager payload 被解析成内部告警对象，再触发 FaultDiagnoseWorkflow，形成告警到处置建议的闭环。

## 录屏建议

1. 先运行 `python -m pytest` 展示质量基线。
2. 依次运行 demo1、demo2、demo4、demo6，控制在 5 分钟内。
3. Web Console 单独录制 30 秒，展示 trace、metrics 和 CloudOps 模式。
4. GIF 放到 `assets/`，README 中引用。