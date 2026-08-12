# 性能测试报告

## 口径说明

本报告只记录可复现实测数据。未完成实测的对比项保留为待补录，避免面试中出现无法解释的数据。

## 当前可执行命令

```powershell
python examples/demo_benchmark.py
python -m pytest
python -m pytest --cov=athena --cov-report=term-missing
```

当前覆盖率采用核心能力口径：排除 CLI/TUI、外部集成适配器、观测 Web 页面，以及文件/Git/代码解析等外部边界工具。完整全仓覆盖率仍需后续补测这些适配层。

## 指标设计

| 指标 | 说明 | 数据来源 |
| --- | --- | --- |
| 记忆检索命中率 | query 是否召回期望 memory/skill | LongTermMemory、SkillLibrary 测试集 |
| 长对话 Token 消耗 | 多轮对话后 prompt token 变化 | WorkingMemory 裁剪前后统计 |
| 工具调用成功率 | ToolResult success 比例 | ToolExecutor / ToolRegistry trace |
| 容错恢复率 | 工具失败后能否换路或给出解释 | 构造失败工具 benchmark |
| 同类 Agent 对比 | Athena 与第三方框架原型的延迟、成功率、Token | 独立 benchmark runner |

## 待补录数据表

| 场景 | Athena | 普通截断 Agent | 第三方框架原型 | 备注 |
| --- | ---: | ---: | ---: | --- |
| Skill 召回命中率 | 待实测 | 待实测 | 待实测 | 使用固定 50 条任务集 |
| 长对话 Token 消耗 | 待实测 | 待实测 | 待实测 | 20 轮对话，每轮同一任务族 |
| 工具调用成功率 | 待实测 | 待实测 | 待实测 | 包含成功、缺参、异常三类工具 |
| 容错恢复率 | 待实测 | 待实测 | 待实测 | 工具故障注入 |
| 平均端到端耗时 | 待实测 | 待实测 | 待实测 | 本机、同模型、同 prompt |

## 面试讲法

如果数据还没补齐，直接说明：“我没有把未验证数字写进 README；目前项目提供 benchmark engine 和可复现命令，下一步会固定任务集后补全趋势数据。”这比背无法复现的数字更可信。