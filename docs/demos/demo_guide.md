# Athena Agent Demo 指南

## 自进化演示

命令：`python examples/demo2_self_evolution.py`

讲解重点：一次复杂任务的轨迹被 ComplexityEvaluator 评分，SkillGenerator 生成可复用 Skill，SkillLibrary 在下次相似 query 中召回。

## K8s 故障自动排查

命令：`python examples/demo4_k8s_diagnose.py`

讲解重点：模拟 CrashLoopBackOff、ImagePullBackOff 和资源压力，通过 K8sDiagnoser 输出根因和建议。

## 告警自动处置

命令：`python examples/demo6_alert_auto_handle.py`

讲解重点：Alertmanager payload 被解析成内部告警对象，再触发 FaultDiagnoseWorkflow，形成告警到处置建议的闭环。

## Multi-Agent 协作

命令：`python examples/demo_multi_agent.py`

讲解重点：规划、执行和校验角色通过受控上下文协作，最终结果仍经过统一的工具和权限边界。

## Web Console

命令：`python examples/demo_web_console.py`

讲解重点：前端通过 API 查看任务状态、Evidence、Token 和执行轨迹。

## Benchmark

命令：`python examples/demo_benchmark.py`

讲解重点：用固定任务集比较步骤数、Token、延迟和成功率，避免用主观感受判断 Agent 效果。

## 录屏建议

1. 先运行 `python -m pytest` 展示质量基线。
2. 依次运行自进化、K8s 诊断和告警处置，控制在 5 分钟内。
3. Web Console 单独录制 30 秒，展示 trace、metrics 和 CloudOps 模式。
