"""Safe adapter for the Anthropic-style ``SKILL.md`` directory format.

The file format is intentionally treated as data. Parsing never executes a
script, grants a capability, or changes a Skill lifecycle state.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from athena.runtime.memory.retrieval import EvaluatedSkill
from athena.runtime.memory.models import SkillEvaluationState


class AnthropicSkillError(ValueError):
    """Raised when a Skill directory violates the external format contract."""


class SkillNotApprovedError(AnthropicSkillError):
    """Raised when an unapproved document is requested for Runtime loading."""


_ALLOWED_FRONTMATTER = frozenset(
    {
        "name",
        "description",
        "license",
        "compatibility",
        "metadata",
        "status",
        "allowed_tools",
        "risk_level",
        "token_budget_hint",
    }
)
_APPROVED_STATUS = "approved"
_NON_EXECUTABLE_STATUS = frozenset({"candidate", "approved"})


@dataclass(frozen=True)
class AnthropicSkillDocument:
    """Parsed Skill metadata and file references, with no executable content."""

    root: Path
    name: str
    description: str
    body: str
    status: str
    references: tuple[str, ...]
    scripts: tuple[str, ...]
    metadata: dict[str, Any]

    @property
    def is_approved(self) -> bool:
        return self.status == _APPROVED_STATUS

    def to_evaluated_skill(self) -> EvaluatedSkill:
        """Convert only an approved document to the Runtime Skill projection."""

        if not self.is_approved:
            raise SkillNotApprovedError("only approved Skills may enter Runtime")
        return EvaluatedSkill(
            skill_id=self.name,
            title=self.name,
            procedure_summary=self.description,
            evaluation_state=SkillEvaluationState.APPROVED,
            source_references=(str(self.root / "SKILL.md"),),
        )

    def to_candidate_payload(self) -> tuple[dict[str, object], dict[str, object]]:
        """Create the existing candidate manifest/procedure shape.

        This projection is always candidate-only. Promotion remains owned by
        the existing validator, replay evaluator, review and release modules.
        """

        manifest: dict[str, object] = {
            "name": self.name,
            "description": self.description,
            "candidate_only": True,
            "activation_allowed": False,
            "readonly": True,
            "creates_tool": False,
            "allowed_tools": list(self.metadata.get("allowed_tools", [])),
            "risk_level": str(self.metadata.get("risk_level", "S1")),
        }
        procedure = {
            "execution_mode": "react",
            "steps": [line.strip() for line in self.body.splitlines() if line.strip()],
            "references": list(self.references),
            "scripts": list(self.scripts),
        }
        return manifest, procedure


class AnthropicSkillLoader:
    """Parse and validate one local ``SKILL.md`` directory."""

    def parse(self, skill_directory: str | Path) -> AnthropicSkillDocument:
        root = Path(skill_directory).expanduser().resolve()
        if not root.is_dir():
            raise AnthropicSkillError("Skill directory does not exist")
        skill_file = root / "SKILL.md"
        if not skill_file.is_file() or skill_file.is_symlink():
            raise AnthropicSkillError("SKILL.md must be a regular file")
        text = skill_file.read_text(encoding="utf-8")
        frontmatter, body = self._split_frontmatter(text)
        unknown = set(frontmatter) - _ALLOWED_FRONTMATTER
        if unknown:
            raise AnthropicSkillError(
                "unknown SKILL.md frontmatter: " + ", ".join(sorted(unknown))
            )
        name = frontmatter.get("name")
        description = frontmatter.get("description")
        if not isinstance(name, str) or not name.strip():
            raise AnthropicSkillError("SKILL.md requires a non-empty name")
        if not isinstance(description, str) or not description.strip():
            raise AnthropicSkillError("SKILL.md requires a non-empty description")
        status = frontmatter.get("status", "candidate")
        if not isinstance(status, str) or status not in _NON_EXECUTABLE_STATUS:
            raise AnthropicSkillError("Skill status must be candidate or approved")
        allowed_tools = frontmatter.get("allowed_tools", [])
        if not isinstance(allowed_tools, list) or any(
            not isinstance(item, str) or not item.strip() for item in allowed_tools
        ):
            raise AnthropicSkillError("allowed_tools must be a list of names")
        references = self._safe_files(root, "references")
        scripts = self._safe_files(root, "scripts")
        return AnthropicSkillDocument(
            root=root,
            name=name.strip(),
            description=" ".join(description.split()),
            body=body.strip(),
            status=status,
            references=references,
            scripts=scripts,
            metadata=dict(frontmatter),
        )

    def load_approved(self, skill_directory: str | Path) -> AnthropicSkillDocument:
        document = self.parse(skill_directory)
        if not document.is_approved:
            raise SkillNotApprovedError("only approved Skills may be loaded")
        return document

    @staticmethod
    def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            raise AnthropicSkillError("SKILL.md must start with YAML frontmatter")
        try:
            closing = lines.index("---", 1)
        except ValueError as exc:
            raise AnthropicSkillError("SKILL.md frontmatter is not closed") from exc
        try:
            value = yaml.safe_load("\n".join(lines[1:closing])) or {}
        except yaml.YAMLError as exc:
            raise AnthropicSkillError("invalid SKILL.md YAML frontmatter") from exc
        if not isinstance(value, dict):
            raise AnthropicSkillError("SKILL.md frontmatter must be a mapping")
        return dict(value), "\n".join(lines[closing + 1 :])

    @staticmethod
    def _safe_files(root: Path, directory_name: str) -> tuple[str, ...]:
        directory = root / directory_name
        if not directory.exists():
            return ()
        if not directory.is_dir() or directory.is_symlink():
            raise AnthropicSkillError(f"{directory_name} must be a directory")
        files: list[str] = []
        for item in sorted(directory.rglob("*")):
            resolved = item.resolve()
            if root not in resolved.parents or item.is_symlink():
                raise AnthropicSkillError(f"{directory_name} contains an unsafe path")
            if item.is_file():
                files.append(item.relative_to(root).as_posix())
        return tuple(files)


__all__ = [
    "AnthropicSkillDocument",
    "AnthropicSkillError",
    "AnthropicSkillLoader",
    "SkillNotApprovedError",
]
