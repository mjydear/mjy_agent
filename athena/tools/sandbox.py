"""
📦 模块名称：安全沙箱（Security Sandbox）
📍 架构位置：工具层安全边界（Tool Safety Boundary）—— 位于工具调用和真实外部世界之间：
              [ToolExecutor / Builtin Tools] → 【SecuritySandbox】 → [Python / Shell / Network]
🎯 核心作用：把所有高风险外部操作收口到一个可审计、可限制、可超时的执行边界里。
              Agent 可以调用工具，但不能无限制地执行任意代码、命令或网络请求。
🔗 依赖关系：
    - 依赖：asyncio、RestrictedPython、子进程、urllib
    - 被依赖：Git 工具、未来的 Python 代码执行工具、网络请求工具
💡 设计思路：
    安全沙箱遵循“宁严勿松”的原则：
    ① Python 执行 → RestrictedPython 限制危险内置能力
    ② Shell 命令 → allowlist 白名单 + dangerous fragment 黑名单双层检查
    ③ 网络请求 → host allowlist，默认不允许访问任何外部站点
    ④ 所有操作 → timeout + output limit + AuditRecord 审计记录

    面试时可以强调：这里不是把安全寄托给 LLM“自觉不乱执行”，而是用代码边界强制约束能力。
📚 学习重点：
    1. 为什么 Agent 工具一定要有沙箱，而不是直接 subprocess/eval
    2. 白名单和黑名单分别解决什么问题，以及为什么要组合使用
    3. timeout 和 max_output_chars 如何防止资源耗尽
    4. AuditRecord 如何支持事后复盘、回滚和安全追责
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class SandboxPolicy:
    """
    沙箱安全策略配置。

    字段说明：
        allowed_shell_commands: 允许执行的命令名集合，不在集合里的命令直接拒绝
        blocked_shell_fragments: 危险片段黑名单，用于拦截组合命令中的破坏性操作
        allowed_hosts: 允许访问的网络主机，默认为空表示禁止网络访问
        timeout_seconds: 单次外部操作最大耗时
        max_output_chars: 最大输出长度，防止工具刷爆上下文或内存
    """

    allowed_shell_commands: frozenset[str] = frozenset(
        {"git", "python", "pytest", "dir", "type", "findstr"}
    )
    blocked_shell_fragments: tuple[str, ...] = (
        "rm -rf",
        "del /s",
        "format ",
        "shutdown",
        "reg delete",
        ":(){",
    )
    allowed_hosts: frozenset[str] = frozenset()
    timeout_seconds: float = 5.0
    max_output_chars: int = 20_000

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_output_chars <= 0:
            raise ValueError("max_output_chars must be positive")


@dataclass(frozen=True)
class AuditRecord:
    """
    一条外部操作审计记录。

    设计思路：
        安全系统不能只回答“成功/失败”，还要回答“谁在什么时候执行了什么”。
        这里记录 operation、command、时间和结果，后续可以落盘、进入 Tracer，或驱动回滚流程。
    """

    operation: str
    command: str
    started_at: float
    finished_at: float
    success: bool
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SandboxResult:
    """沙箱执行结果，统一封装输出、错误和审计记录。"""

    success: bool
    output: str
    error: str | None = None
    audit: AuditRecord | None = None


class SecuritySandbox:
    """
    外部操作的统一安全边界。

    功能说明：
        run_python()、run_shell()、fetch_url() 分别覆盖三类常见高风险能力。
        它们都遵循同一个原则：先校验策略，再执行，再记录审计。

    # 🎯 面试考点：为什么安全逻辑不放在每个工具函数里？
    # 答：分散在每个工具里容易漏；收口到 SecuritySandbox 能统一策略、统一审计、统一测试。
    """

    def __init__(
        self, policy: SandboxPolicy | None = None, audit_log_path: Path | None = None
    ) -> None:
        self.policy = policy or SandboxPolicy()
        self.audit_log_path = audit_log_path
        self.audit_records: list[AuditRecord] = []

    async def run_python(self, code: str) -> SandboxResult:
        """
        在 RestrictedPython 环境中执行代码。

        注意：
            这里用于受限表达式和小段脚本，不适合执行完整项目代码。
            真正的生产沙箱还应叠加进程级 CPU/内存限制或容器隔离。
        """
        self._require_text(code, "code")
        started_at = time.time()
        try:
            output = await asyncio.wait_for(
                asyncio.to_thread(self._execute_restricted_python, code),
                timeout=self.policy.timeout_seconds,
            )
            return self._result(
                "python", "<restricted-python>", started_at, True, output
            )
        except Exception as exc:
            return self._result(
                "python", "<restricted-python>", started_at, False, "", str(exc)
            )

    async def run_shell(self, command: str, cwd: Path | None = None) -> SandboxResult:
        """
        执行受控 Shell 命令。

        安全流程：
            1. 检查命令非空
            2. 黑名单拦截危险片段
            3. 白名单确认可执行程序
            4. asyncio timeout 限制执行时间
            5. 截断输出并记录审计
        """
        command = self._require_text(command, "command")
        self._validate_shell_command(command)
        started_at = time.time()
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(cwd) if cwd else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.policy.timeout_seconds
            )
            output = (stdout + stderr).decode("utf-8", errors="replace")[
                : self.policy.max_output_chars
            ]
            return self._result(
                "shell",
                command,
                started_at,
                process.returncode == 0,
                output,
                None if process.returncode == 0 else output,
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            return self._result(
                "shell", command, started_at, False, "", "command timed out"
            )

    async def fetch_url(self, url: str) -> SandboxResult:
        """只允许访问显式 allowlist 中的主机，默认禁止所有网络请求。"""
        url = self._require_text(url, "url")
        host = urlparse(url).hostname or ""
        if host not in self.policy.allowed_hosts:
            raise PermissionError(f"host is not allowlisted: {host}")
        started_at = time.time()
        try:

            def read() -> str:
                with urllib.request.urlopen(
                    url, timeout=self.policy.timeout_seconds
                ) as response:
                    return response.read(self.policy.max_output_chars).decode(
                        "utf-8", errors="replace"
                    )

            output = await asyncio.wait_for(
                asyncio.to_thread(read), timeout=self.policy.timeout_seconds
            )
            return self._result("network", url, started_at, True, output)
        except Exception as exc:
            return self._result("network", url, started_at, False, "", str(exc))

    def _execute_restricted_python(self, code: str) -> str:
        try:
            from RestrictedPython import compile_restricted
            from RestrictedPython.Guards import safe_builtins
        except ImportError as exc:
            raise RuntimeError(
                "RestrictedPython is required for sandboxed Python execution"
            ) from exc

        byte_code = compile_restricted(code, "<sandbox>", "exec")
        namespace: dict[str, Any] = {
            "__builtins__": safe_builtins,
            "_print_": _SandboxPrinter,
        }
        exec(byte_code, namespace, namespace)
        printer = namespace.get("_print")
        return printer() if callable(printer) else ""

    def _validate_shell_command(self, command: str) -> None:
        lowered = command.lower()
        if any(fragment in lowered for fragment in self.policy.blocked_shell_fragments):
            raise PermissionError("command contains a blocked fragment")
        parts = shlex.split(command, posix=False)
        if not parts:
            raise ValueError("command must not be empty")
        executable = Path(parts[0]).name.lower().removesuffix(".exe")
        if executable not in self.policy.allowed_shell_commands:
            raise PermissionError(f"command is not allowlisted: {executable}")

    def _result(
        self,
        operation: str,
        command: str,
        started_at: float,
        success: bool,
        output: str,
        error: str | None = None,
    ) -> SandboxResult:
        record = AuditRecord(
            operation=operation,
            command=command,
            started_at=started_at,
            finished_at=time.time(),
            success=success,
        )
        self.audit_records.append(record)
        if self.audit_log_path is not None:
            self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record.__dict__, ensure_ascii=False) + "\n")
        return SandboxResult(success=success, output=output, error=error, audit=record)

    def _require_text(self, value: str, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        return value.strip()


class _SandboxPrinter:
    def __init__(self, _getattr_: object | None = None) -> None:
        self._parts: list[str] = []

    def _call_print(self, *objects: object, **kwargs: object) -> None:
        sep = str(kwargs.get("sep", " "))
        end = str(kwargs.get("end", "\n"))
        self._parts.append(sep.join(str(obj) for obj in objects) + end)

    def __call__(self) -> str:
        return "".join(self._parts)
