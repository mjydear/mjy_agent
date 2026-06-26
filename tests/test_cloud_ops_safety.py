"""Safety and resilience tests for CloudOps tools."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

import pytest

from athena.tools import SecuritySandbox
from athena.tools.builtin.cloud import (
    AliyunClient,
    CloudOperation,
    CloudProviderClient,
    CloudRiskLevel,
)
from athena.tools.builtin.k8s import K8sOpsTools
from athena.types import JSONValue


def test_cloud_high_risk_operation_is_blocked_without_confirmation() -> None:
    client = AliyunClient()

    result = client.restart_instance("i-prod-api-01", confirmed=False)

    assert result.success is False
    assert result.requires_confirmation is True
    assert result.risk_level is CloudRiskLevel.HIGH


def test_cloud_provider_retries_and_returns_fallback_failure() -> None:
    client = CloudProviderClient(max_retries=2)
    operation = CloudOperation(name="unstable_call", risk_level=CloudRiskLevel.READ)

    def failing_handler() -> dict[str, JSONValue]:
        raise ConnectionError("temporary network failure")

    result = client.execute(operation, failing_handler)

    assert result.success is False
    assert result.message == "operation failed after retries"
    assert "temporary network failure" in str(result.data["error"])


def test_k8s_yaml_generation_and_validation() -> None:
    tools = K8sOpsTools()

    manifest = tools.generate_deployment_yaml(
        name="demo-api", image="nginx:1.25", replicas=2
    )
    valid = tools.validate_yaml(manifest)
    invalid = tools.validate_yaml("kind: Pod\nmetadata:\n  name: missing-spec")

    assert valid["valid"] is True
    assert invalid["valid"] is False
    assert "spec" in cast(Sequence[JSONValue], invalid["missing_fields"])


@pytest.mark.asyncio
async def test_sandbox_blocks_dangerous_k8s_shell_operation() -> None:
    sandbox = SecuritySandbox()

    with pytest.raises(PermissionError):
        await sandbox.run_shell("kubectl delete pod checkout-5f8b")
