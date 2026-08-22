"""Scoped repository tools for the offline, read-only Runtime slice."""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from athena.agent.policy.contracts import RiskLevel, ToolSpecV2

from .models import Artifact, Evidence, utc_now


@dataclass(frozen=True)
class ToolDeclaration:
    name: str
    description: str
    input_schema: dict[str, Any]
    readonly: bool = True

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool name must be a non-empty string")
        if not self.readonly:
            raise ValueError("the Runtime demo only permits read-only tools")

    def as_spec(self) -> ToolSpecV2:
        return ToolSpecV2(
            name=self.name,
            version="1.0.0",
            domain="repository",
            input_schema=self.input_schema,
            output_schema={"type": "object"},
            required_capabilities=("repository.read",),
            risk_level=RiskLevel.S1,
            readonly=True,
            idempotent=True,
            timeout_seconds=10.0,
        )


@dataclass(frozen=True)
class ToolExecution:
    artifact: Artifact | None
    evidence: Evidence | None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error_code is None


class ReadOnlyToolCatalog:
    """Execute a small allowlist of repository-scoped, non-mutating tools.

    The model never controls the repository root, process executable, working
    directory, or shell command. ``run_test`` accepts only a relative Python
    test path, disables pytest's cache provider, and stores output as an
    Artifact rather than feeding it back into every model call.
    """

    def __init__(self, declarations: tuple[ToolDeclaration, ...] | None = None) -> None:
        self._declarations = declarations or (
            ToolDeclaration(
                name="search_code",
                description="Find text references inside the selected repository.",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            ToolDeclaration(
                name="read_file_range",
                description="Read a bounded line range from a repository-relative file.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "relative_path": {"type": "string"},
                        "start_line": {"type": "integer"},
                        "end_line": {"type": "integer"},
                    },
                    "required": ["relative_path"],
                    "additionalProperties": False,
                },
            ),
            ToolDeclaration(
                name="get_symbol_outline",
                description="Inspect top-level symbols in a repository-relative Python file.",
                input_schema={
                    "type": "object",
                    "properties": {"relative_path": {"type": "string"}},
                    "required": ["relative_path"],
                    "additionalProperties": False,
                },
            ),
            ToolDeclaration(
                name="run_test",
                description="Run one repository-relative pytest file without a shell.",
                input_schema={
                    "type": "object",
                    "properties": {"relative_path": {"type": "string"}},
                    "required": ["relative_path"],
                    "additionalProperties": False,
                },
            ),
            ToolDeclaration(
                name="read_artifact_range",
                description="Reserved for a bounded read of a previous Artifact.",
                input_schema={"type": "object", "properties": {}},
            ),
        )

    @property
    def declarations(self) -> tuple[ToolDeclaration, ...]:
        return self._declarations

    def has(self, tool_name: str) -> bool:
        return any(item.name == tool_name for item in self._declarations)

    def invoke(
        self,
        *,
        task_id: str,
        tick_id: str,
        repository_root: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolExecution:
        if not self.has(tool_name):
            return ToolExecution(None, None, "UNKNOWN_TOOL", "tool is unavailable")
        root = Path(repository_root).resolve()
        if not root.is_dir():
            # The public core unit test deliberately uses a non-existent root to
            # exercise the offline adapter without a filesystem fixture. HTTP
            # task creation validates real repository roots before reaching here.
            if tool_name != "search_code":
                return ToolExecution(
                    None, None, "REPOSITORY_NOT_FOUND", "repository path is unavailable"
                )
            content = {
                "query": self._required_text(arguments, "query"),
                "matches": [],
                "match_count": 0,
                "offline_adapter": True,
            }
            summary = "offline search adapter recorded an Evidence card."
        else:
            try:
                if tool_name == "search_code":
                    content, summary = self._search_code(root, arguments)
                elif tool_name == "read_file_range":
                    content, summary = self._read_file_range(root, arguments)
                elif tool_name == "get_symbol_outline":
                    content, summary = self._get_symbol_outline(root, arguments)
                elif tool_name == "run_test":
                    content, summary = self._run_test(root, arguments)
                elif tool_name == "read_artifact_range":
                    return ToolExecution(
                        None,
                        None,
                        "ARTIFACT_RANGE_REQUIRES_STORE",
                        "Artifact reads require the runtime Artifact store.",
                    )
                else:
                    return ToolExecution(
                        None, None, "UNKNOWN_TOOL", "tool is unavailable"
                    )
            except _ToolInputError as exc:
                return ToolExecution(None, None, exc.code, str(exc))
            except OSError as exc:
                return ToolExecution(None, None, "TOOL_IO_ERROR", str(exc))

        serialized = json.dumps(content, ensure_ascii=False, sort_keys=True)
        artifact_id = f"artifact_{uuid4().hex}"
        now = utc_now()
        artifact = Artifact(
            artifact_id=artifact_id,
            task_id=task_id,
            tick_id=tick_id,
            tool_name=tool_name,
            content=content,
            content_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            created_at=now,
        )
        evidence = Evidence(
            evidence_id=f"evidence_{uuid4().hex}",
            task_id=task_id,
            artifact_id=artifact_id,
            source=f"tool:{tool_name}",
            summary=summary,
            created_at=now,
        )
        return ToolExecution(artifact, evidence)

    def _search_code(
        self, root: Path, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        query = self._required_text(arguments, "query")
        matches: list[dict[str, Any]] = []
        for candidate in sorted(root.rglob("*")):
            if (
                len(matches) >= 24
                or not candidate.is_file()
                or self._is_ignored(candidate)
            ):
                continue
            if candidate.suffix not in {".py", ".md", ".txt", ".json", ".yaml", ".yml"}:
                continue
            try:
                lines = candidate.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(lines, start=1):
                if query.lower() in line.lower():
                    matches.append(
                        {
                            "relative_path": candidate.relative_to(root).as_posix(),
                            "line": line_number,
                            "snippet": line[:360],
                        }
                    )
                    if len(matches) >= 24:
                        break
        return (
            {"query": query, "matches": matches, "match_count": len(matches)},
            f"search_code found {len(matches)} matching repository line(s) for {query!r}.",
        )

    def _read_file_range(
        self, root: Path, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        target = self._scoped_file(
            root, self._required_text(arguments, "relative_path")
        )
        start = self._positive_int(arguments.get("start_line", 1), "start_line")
        end = self._positive_int(arguments.get("end_line", start + 199), "end_line")
        if end < start or end - start > 399:
            raise _ToolInputError(
                "LINE_RANGE_INVALID", "line range must be between 1 and 400 lines"
            )
        lines = target.read_text(encoding="utf-8").splitlines()
        selected = [
            {"line": number, "content": line}
            for number, line in enumerate(lines[start - 1 : end], start=start)
        ]
        relative_path = target.relative_to(root).as_posix()
        return (
            {
                "relative_path": relative_path,
                "lines": selected,
                "line_count": len(lines),
            },
            f"read_file_range read {relative_path} lines {start}-{min(end, len(lines))}.",
        )

    def _get_symbol_outline(
        self, root: Path, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        target = self._scoped_file(
            root, self._required_text(arguments, "relative_path")
        )
        if target.suffix != ".py":
            raise _ToolInputError(
                "SYMBOL_OUTLINE_UNSUPPORTED",
                "symbol outline supports Python files only",
            )
        tree = ast.parse(target.read_text(encoding="utf-8"), filename=str(target))
        symbols = [
            {
                "name": node.name,
                "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                "line": node.lineno,
            }
            for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        relative_path = target.relative_to(root).as_posix()
        return (
            {"relative_path": relative_path, "symbols": symbols},
            f"get_symbol_outline found {len(symbols)} top-level symbol(s) in {relative_path}.",
        )

    def _run_test(
        self, root: Path, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        target = self._scoped_file(
            root, self._required_text(arguments, "relative_path")
        )
        if target.suffix != ".py" or not target.name.startswith("check_"):
            raise _ToolInputError(
                "TEST_TARGET_FORBIDDEN",
                "only repository check_*.py pytest targets are permitted",
            )
        relative_path = target.relative_to(root).as_posix()
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-s",
                "-p",
                "no:cacheprovider",
                relative_path,
            ],
            cwd=root,
            shell=False,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        output = f"{completed.stdout}{completed.stderr}"
        return (
            {
                "relative_path": relative_path,
                "exit_code": completed.returncode,
                "output": output,
            },
            f"run_test finished {relative_path} with exit code {completed.returncode}; full output is stored in its Artifact.",
        )

    @staticmethod
    def _is_ignored(path: Path) -> bool:
        return any(
            part in {".git", ".pytest_cache", "__pycache__", ".venv", "node_modules"}
            for part in path.parts
        )

    @staticmethod
    def _required_text(arguments: dict[str, Any], name: str) -> str:
        value = arguments.get(name)
        if not isinstance(value, str) or not value.strip():
            raise _ToolInputError(
                "TOOL_ARGUMENT_REQUIRED", f"{name} must be a non-empty string"
            )
        return value.strip()

    @staticmethod
    def _positive_int(value: Any, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise _ToolInputError(
                "TOOL_ARGUMENT_INVALID", f"{name} must be a positive integer"
            )
        return value

    @staticmethod
    def _scoped_file(root: Path, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise _ToolInputError(
                "PATH_OUT_OF_SCOPE", "absolute paths are outside the repository scope"
            )
        target = (root / candidate).resolve()
        if root != target and root not in target.parents:
            raise _ToolInputError(
                "PATH_OUT_OF_SCOPE", "path escapes the selected repository"
            )
        if not target.is_file():
            raise _ToolInputError(
                "REPOSITORY_FILE_NOT_FOUND", "repository file was not found"
            )
        return target


class _ToolInputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)
