"""Prompt context assembly for Athena's ReAct loop."""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from athena.exceptions import ErrorCode, PromptError
from athena.memory import WorkingMemory
from athena.tools import ToolRegistry
logger = logging.getLogger(__name__)


class ContextAssembler(BaseModel):
    """Build the full prompt sent to the LLM for each agent step."""

    system_prompt_path: Path = Path("prompts/system/base.md")
    template_path: Path = Path("athena/prompt/templates/react.md")
    static_context: str = ""
    output_contract: str = Field(
        default=(
            "Return JSON only with keys: thought, action, action_input, final_answer. "
            "Use action=null when final_answer is ready."
        )
    )

    def build_prompt(
        self,
        query: str,
        memory: WorkingMemory,
        tools: ToolRegistry,
        scratchpad: str,
    ) -> str:
        """Build a ReAct prompt from stable context and current state.

        Args:
            query: User request.
            memory: Short-term memory provider.
            tools: Registered tool registry.
            scratchpad: Previous thoughts/actions/observations in this run.

        Returns:
            Fully assembled prompt string.

        Raises:
            PromptError: If prompt files cannot be loaded.
        """
        try:
            system_prompt = self._read_optional(self.system_prompt_path)
            template = self._read_optional(self.template_path)
            tool_descriptions = tools.describe_tools()
            return template.format(
                system_prompt=system_prompt,
                static_context=self.static_context,
                memory=memory.render(),
                tools=tool_descriptions,
                scratchpad=scratchpad,
                query=query,
                output_contract=self.output_contract,
            )
        except (OSError, KeyError) as exc:
            logger.exception("Prompt assembly failed")
            raise PromptError(ErrorCode.PROMPT_BUILD_FAILED, str(exc)) from exc

    def _read_optional(self, path: Path) -> str:
        """Read a prompt file if it exists, otherwise return an empty section."""
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()
