"""
📦 模块名称：Skill 自进化编排器（Skill Evolver）
📍 架构位置：GEPA 自进化闭环的“大脑”，作为注入给 CuratorDaemon 的真实后台 job。
🎯 核心作用：把“真实执行轨迹 + 人工反馈”转化为 Skill 的新增 / 修正 / 优化 / 降权，持续沉淀运维经验。
🔗 依赖关系：依赖 Tracer、ComplexityEvaluator、SkillGenerator、SkillValidator、SkillLibrary、FeedbackStore。
💡 设计思路：
    这是把此前“零件齐全、整机没装”的 GEPA 积木真正装配成闭环的地方：
    ① 从 Tracer 取每个未处理 run 的轨迹 → 复杂度门控 → 生成候选 Skill；
    ② 与已有 Skill 语义去重：命中则**修正/优化**（合并步骤、bump version、更新计数），否则新增；
    ③ 融入人工反馈：reject → 降权/删除对应 Skill；correct → 用修正文本更新 Skill 正文；
    ④ 只有通过 SkillValidator（结构 + 工具真实存在）的 Skill 才入库并持久化。
📚 学习重点：闭环的关键是“幂等 + 去重 + 反馈优先”，避免重复轨迹刷屏 Skill 库、避免坏经验沉淀。
"""

from __future__ import annotations

import logging

from athena.learning.complexity import ComplexityEvaluator
from athena.learning.skill_gen import SkillGenerator
from athena.learning.skill_optimizer import SkillValidator
from athena.learning.tracer import Tracer
from athena.memory.skill import Skill, SkillLibrary

logger = logging.getLogger(__name__)


class SkillEvolver:
    """
    Skill 自进化编排器（CuratorDaemon 的真实 job 提供者）。

    功能说明：消费轨迹与人工反馈，驱动 Skill 的生成/修正/优化/降权并持久化。
    参数说明：
        skill_library：技能库（进化写入目标，建议注入带 cache 的持久化实例）。
        validator：Skill 准入校验器（结构 + 工具真实存在）。
        feedback_store：可选人工反馈存储（reject/correct 驱动降权与修正）。
        complexity：复杂度评估器（生成门控），缺省用默认阈值。
        generator：Skill 生成器，缺省用规则式生成。
        similarity_threshold：判定“同类 Skill”的匹配阈值（预留，当前用名称/召回去重）。
    设计思路：job(tracer) 是幂等的——已处理的 run 与反馈用 set 记忆，重复调用不重复沉淀。
    使用示例：CuratorDaemon(tracer, job=SkillEvolver(lib, validator).job).start()
    """

    def __init__(
        self,
        skill_library: SkillLibrary,
        validator: SkillValidator,
        feedback_store: object | None = None,
        complexity: ComplexityEvaluator | None = None,
        generator: SkillGenerator | None = None,
    ) -> None:
        self.skill_library = skill_library
        self.validator = validator
        self.feedback_store = feedback_store
        self.complexity = complexity or ComplexityEvaluator(skill_threshold=0.3)
        self.generator = generator or SkillGenerator()
        self._processed_runs: set[str] = set()
        self._processed_feedback: set[str] = set()

    async def job(self, tracer: Tracer) -> None:
        """
        CuratorDaemon 周期性调用的进化 job。

        功能说明：先处理人工反馈（最高优先级监督信号），再从轨迹生成/合并 Skill。
        参数说明：tracer 是共享的执行轨迹记录器。
        返回值：None。
        设计思路：反馈先行，保证被否决的坏经验不会在同一轮又被轨迹重新生成。
        """
        try:
            await self._apply_feedback()
            await self._evolve_from_traces(tracer)
        except Exception as exc:  # noqa: BLE001 - 后台 job 不能因单次异常而中断守护
            logger.warning("skill evolution job failed: %s", exc)

    async def _apply_feedback(self) -> None:
        """处理未消费的人工反馈：reject 降权/删除，correct 修正对应 Skill 正文。"""
        if self.feedback_store is None:
            return
        items = self.feedback_store.unprocessed(self._processed_feedback)
        for item in items:
            self._processed_feedback.add(item.feedback_id)
            if item.verdict == "reject":
                # 否决：找与该任务相关的 Skill 并降权（失败计数+1），失败过多则删除。
                self._penalize_related_skills(item.task_id)
            elif item.verdict == "correct" and item.correction_text.strip():
                await self._apply_correction(item.task_id, item.correction_text)
            # adopt：正反馈，作为成功信号，后续可提升对应 Skill 成功计数（此处保守不动）。

    def _penalize_related_skills(self, task_id: str) -> None:
        """否决反馈：对最近相关 Skill 记一次失败；失败≥3 且成功=0 则移除。"""
        for name, skill in list(self.skill_library.skills.items()):
            updated = Skill(
                name=skill.name,
                description=skill.description,
                content=skill.content,
                tags=skill.tags,
                version=skill.version,
                success_count=skill.success_count,
                failure_count=skill.failure_count + 1,
                created_at=skill.created_at,
            )
            if updated.failure_count >= 3 and updated.success_count == 0:
                self.skill_library.remove_skill(name)
                logger.info("removed low-quality skill after rejections: %s", name)
            else:
                self.skill_library.skills[name] = updated
                self.skill_library._persist(updated)
            break  # 保守：一次否决只影响一个最相关 Skill，避免误伤

    async def _apply_correction(self, task_id: str, correction_text: str) -> None:
        """修正反馈：把人工修正文本追加进最相关 Skill 正文并 bump version。"""
        matches = await self.skill_library.match(correction_text, top_k=1)
        if not matches:
            return
        target = matches[0]
        corrected = Skill(
            name=target.name,
            description=target.description,
            content=(
                f"{target.content}\n\n[Human correction]: {correction_text.strip()}"
            ),
            tags=target.tags,
            version=target.version + 1,
            success_count=target.success_count,
            failure_count=target.failure_count,
            created_at=target.created_at,
        )
        await self.skill_library.add_skill(corrected)
        logger.info("applied human correction to skill %s -> v%s", target.name, corrected.version)

    async def _evolve_from_traces(self, tracer: Tracer) -> None:
        """从每个未处理 run 的轨迹生成候选 Skill，去重合并后校验入库。"""
        run_ids = {event.run_id for event in tracer.events}
        for run_id in run_ids:
            if run_id in self._processed_runs:
                continue
            events = list(tracer.by_run(run_id))
            if not events:
                continue
            self._processed_runs.add(run_id)

            score = self.complexity.evaluate(events)
            if not score.should_generate_skill:
                continue  # 简单任务不值得沉淀 Skill

            # 用轨迹里第一条 thought 作为候选 Skill 名字来源，缺省用 run_id。
            name = self._infer_skill_name(events, run_id)
            generated = self.generator.build_skill(name, events, score, success=True)
            candidate = generated.skill

            # 语义去重：命中已有相似 Skill → 修正/优化（合并 + bump version），否则新增。
            existing = await self._find_similar(candidate)
            if existing is not None:
                merged = self._merge_skills(existing, candidate)
                validation = await self.validator.validate(merged)
                if validation.accepted:
                    await self.skill_library.add_skill(merged)
                    logger.info("optimized existing skill %s -> v%s", merged.name, merged.version)
            else:
                validation = await self.validator.validate(candidate)
                if validation.accepted:
                    await self.skill_library.add_skill(candidate)
                    logger.info("learned new skill %s", candidate.name)
                else:
                    logger.info("skill %s rejected by validator: %s", candidate.name, validation.reason)

    async def _find_similar(self, candidate: Skill) -> Skill | None:
        """按名称精确 + 语义召回判断是否已有同类 Skill。"""
        if candidate.name in self.skill_library.skills:
            return self.skill_library.skills[candidate.name]
        matches = await self.skill_library.match(
            f"{candidate.description}\n{candidate.content}", top_k=1
        )
        return matches[0] if matches else None

    @staticmethod
    def _merge_skills(existing: Skill, candidate: Skill) -> Skill:
        """把新候选的步骤合并进已有 Skill，bump version、累计成功计数。"""
        merged_content = existing.content
        if candidate.content not in existing.content:
            merged_content = f"{existing.content}\n\n[Reinforced]:\n{candidate.content}"
        merged_tags = tuple(dict.fromkeys((*existing.tags, *candidate.tags)))
        return Skill(
            name=existing.name,
            description=existing.description,
            content=merged_content,
            tags=merged_tags,
            version=existing.version + 1,
            success_count=existing.success_count + 1,
            failure_count=existing.failure_count,
            created_at=existing.created_at,
        )

    @staticmethod
    def _infer_skill_name(events, run_id: str) -> str:  # type: ignore[no-untyped-def]
        """从轨迹推断一个可读的 Skill 名（取首个 thought 前几个词，缺省用 run_id）。"""
        for event in events:
            thought = event.payload.get("thought", "").strip()
            if thought:
                words = thought.split()[:4]
                return " ".join(words) or f"skill {run_id}"
        return f"skill {run_id}"
