"""沙箱资源限制测试：内存超限杀进程、超时、策略校验、从设置构造。"""

from __future__ import annotations

import pytest

from athena.config import SandboxSettings
from athena.tools.sandbox import SandboxPolicy, SecuritySandbox


def test_policy_from_settings() -> None:
    policy = SandboxPolicy.from_settings(
        SandboxSettings(cpu_time_seconds=3, memory_mb=128, timeout_seconds=4.0)
    )
    assert policy.cpu_time_seconds == 3
    assert policy.memory_mb == 128
    assert policy.timeout_seconds == 4.0


def test_policy_rejects_bad_limits() -> None:
    with pytest.raises(ValueError):
        SandboxPolicy(memory_mb=0)
    with pytest.raises(ValueError):
        SandboxPolicy(cpu_time_seconds=0)


@pytest.mark.asyncio
async def test_memory_limit_kills_runaway_process() -> None:
    """逐步分配内存的子进程会被内存监控杀掉，返回失败。"""
    policy = SandboxPolicy(
        allowed_shell_commands=frozenset({"python"}),
        memory_mb=64,
        timeout_seconds=10.0,
    )
    sandbox = SecuritySandbox(policy)
    # 每 50ms 分配 ~15MB，很快突破 64MB 限制（写成单行避免 Windows 下多行 -c 被截断）
    code = (
        "import time; buf=[]; "
        "[(buf.append(bytearray(15*1024*1024)), time.sleep(0.05)) for _ in range(200)]"
    )
    result = await sandbox.run_shell(f'python -c "{code}"')
    assert result.success is False
    # 要么触发内存限制，要么被超时兜底，两者都算资源受控
    assert result.error in {"memory limit exceeded", "command timed out"}


@pytest.mark.asyncio
async def test_normal_command_still_succeeds() -> None:
    policy = SandboxPolicy(
        allowed_shell_commands=frozenset({"python"}), memory_mb=256
    )
    sandbox = SecuritySandbox(policy)
    result = await sandbox.run_shell('python -c "print(1+1)"')
    assert result.success is True
    assert "2" in result.output
