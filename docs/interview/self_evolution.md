# 自进化数据闭环面试稿

## 一句话定义

我把自进化做成了一个受治理的数据飞轮：不是让 Agent 把每次回答直接变成 Skill，而是把“成功且被人工确认”的执行轨迹加工成可训练、可评估的数据集，再通过 Replay、Shadow 和人工审核决定是否沉淀为 Skill 草稿。

## 为什么这样设计

直接把 LLM 输出写入长期记忆或 Skill 有三个风险：

1. 单次结果可能错，错误会被放大。
2. 工具原始日志、敏感字段和隐藏思维不应进入训练数据。
3. 没有离线评测和线上观测，无法知道沉淀是否真的提升成功率或降低 Token。

因此，数据集是自进化的中间产物，Skill 只是通过门禁后的人工交接结果。

## 数据流

~~~text
RuntimeSnapshot 成功终态
  + 已验证且接受的 OperatorFeedback
  + >= 3 条 FinalReport Evidence
      -> TrajectoryDatasetBuilder
      -> 脱敏、裁剪、结构校验
      -> 轨迹语义指纹去重
      -> train / validation / test 确定性切分
      -> training-ready JSONL
      -> Candidate
      -> Replay 无副作用评估
      -> Shadow 无副作用观测
      -> 人工 Review
      -> Handoff
      -> 人工创建 Skill 草稿
~~~

## 一条样本长什么样

模型输入只包含稳定语义：

~~~json
{
  "task_goal": "诊断价格计算失败",
  "task_profile": "standard",
  "evidence_refs": [
    {"source": "tool:search_code", "summary": "命中价格计算函数"},
    {"source": "tool:read_file_range", "summary": "读取 pricing.py"},
    {"source": "tool:run_test", "summary": "目标测试失败"}
  ],
  "constraints": ["readonly_tools_only", "evidence_backed_conclusion"]
}
~~~

监督目标包含可解释的行为：

~~~json
{
  "tool_sequence": [
    {"tool_name": "search_code", "reason_code": "DEMO_CODE_DIAGNOSIS"},
    {"tool_name": "read_file_range", "reason_code": "INSPECT_SOURCE_IMPLEMENTATION"}
  ],
  "root_cause": "折扣函数实现错误",
  "repair_recommendation": "修改百分比计算并补充测试",
  "termination": "succeeded"
}
~~~

Evidence ID、Task ID、Feedback ID 只保留在 provenance/audit 元数据中，不放进模型输入，避免模型把每次运行的随机 ID 当成规律。

## 数据质量门禁

一条轨迹要成为样本，必须同时满足：

| 门禁 | 原因 |
| --- | --- |
| 任务终态为 succeeded | 失败轨迹不能作为正样本 |
| 人工反馈 accepted + verified | 结果不能只靠模型自证 |
| FinalReport 至少 3 条可验证 Evidence | 输出必须可追溯 |
| 有结构化工具序列 | 让样本学习可执行步骤，而非散文回答 |
| 脱敏后有公开摘要 | 密钥、Token、密码和隐藏思维不出运行边界 |
| 语义指纹不重复 | 防止相同案例淹没训练集 |

训练输入永远不包含完整 Artifact、工具原始长日志、服务端控制参数或隐藏思维。

## 切分与评估

数据集按语义指纹确定性切分为 train / validation / test。这样同一语义案例会稳定落在同一分区，避免同一问题的近重复样本同时进入训练集和测试集。

训练本身分两种路线：

1. Skill 路线：用数据集抽取候选流程，Replay 验证预期根因，Shadow 观察真实场景但禁止副作用，人工审核后才创建 Skill 草稿。
2. 模型路线：当有足够真实、多样的样本后，将 JSONL 作为 SFT 或偏好数据输入；训练后只在 test split 上对比成功率、工具合法率、平均 Tick、上下文 Token 和人工接管率。

当前项目实现的是数据工程、Skill 路线和评估门禁，不宣称已经用 10 条本地样本完成模型微调。10 条样本只用于证明链路可跑通，真实微调需要更大规模、更多任务族和严格的离线评测。

## 当前可讲指标

运行下面命令会生成最新数字：

~~~powershell
python scripts/run_runtime_benchmark.py --runs 10
~~~

需要关注的自进化指标：

- training-ready 样本数。
- 质量门禁拒绝数及原因。
- 重复轨迹去重数。
- train / validation / test 分布。
- 原始 Artifact 泄漏数，目标为 0。
- 隐藏思维泄漏数，目标为 0。
- Replay / Shadow 通过率。
- 自动激活次数，必须为 0。

## 简历表述

~~~text
构建 Agent 自进化数据闭环：将成功且经人工确认的 Runtime 轨迹标准化为 training-ready JSONL，
通过 Evidence 门禁、敏感信息脱敏、语义指纹去重和 train/validation/test 切分治理数据质量；
结合 Replay、Shadow 与人工审核，保证 Skill 不自动上线。
~~~

## 追问回答

问：这算训练模型吗？

答：它是训练前的数据工程和训练后的评估闭环。目前我没有把少量 Demo 数据包装成“微调了模型”。我先把数据契约、脱敏、去重、切分和 Replay/Shadow 做成可验证系统；数据规模和任务覆盖足够后，JSONL 可以直接进入 SFT 或偏好优化流程，并以 test split 作为唯一效果判断依据。

问：为什么不让 Skill 自动激活？

答：Agent 的工具调用会影响外部系统。即使离线评估通过，也需要 Shadow 观察、人工审核和受控发布；自动激活次数为 0 是安全性质，不是缺少功能。
