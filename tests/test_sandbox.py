"""Sandbox safety tests."""

from __future__ import annotations

import pytest

from athena.tools import SandboxPolicy, SecuritySandbox


@pytest.mark.asyncio
async def test_shell_sandbox_blocks_non_allowlisted_command() -> None:
    sandbox = SecuritySandbox(SandboxPolicy(allowed_shell_commands=frozenset({"git"})))

    with pytest.raises(PermissionError):
        await sandbox.run_shell("python -c print(1)")


@pytest.mark.asyncio
async def test_shell_sandbox_blocks_dangerous_fragment() -> None:
    sandbox = SecuritySandbox()

    with pytest.raises(PermissionError):
        await sandbox.run_shell("git status && rm -rf .")


@pytest.mark.asyncio
async def test_network_sandbox_requires_allowlisted_host() -> None:
    sandbox = SecuritySandbox()

    with pytest.raises(PermissionError):
        await sandbox.fetch_url("https://example.com")