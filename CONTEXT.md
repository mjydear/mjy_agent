# Athena Runtime Context

## Task

一个可持久化、可恢复、可取消的用户目标。Task 由多个 Tick 推进，并拥有明确的状态、预算和租户范围。

## Tick

一次有界的 ReAct 推进。Tick 产生一个公开的 Decision，并至多执行一个逻辑行动；它不是模型的隐藏推理过程。

## Decision

Runtime 接受的结构化选择：`tool_call`、`final`、`ask_human` 或 `fail`。

## Evidence

由工具输出得到、带来源引用并可验证的事实。Evidence 是最终报告结论的依据。

## Artifact

工具输出的原始不可变载荷，例如日志和文件片段。Artifact 默认不直接进入模型上下文。

## Context Snapshot

一次模型调用所需信息的临时编译视图。它不是数据库历史，也不是长期记忆。

## Working Memory

当前 Task 的可恢复工作状态，包括计划、未解决问题和 Evidence 引用。

## Skill

经过评测、治理和版本控制后可复用的程序化流程。Skill 不是一段未验证的 Prompt。

## Policy Config

租户权限、工具范围、风险规则和模型预算的治理配置。它不属于 Memory。
