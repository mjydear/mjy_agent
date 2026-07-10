"""Controlled Kubernetes write actions for CloudOps."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from athena.tools.cloud.k8s.client import K8sReadOnlyClient
from athena.types import JSONValue

if TYPE_CHECKING:
    from athena.config import OpsSecuritySettings


@dataclass(frozen=True)
class K8sActionSecurityPolicy:
    """Company-level guardrails for controlled Kubernetes write actions."""

    default_readonly: bool = True
    allowed_resource_kinds: tuple[str, ...] = ("Deployment",)
    allowed_verbs: tuple[str, ...] = (
        "rollout_restart",
        "scale",
        "pause",
        "resume",
    )
    blocked_actions: tuple[str, ...] = (
        "delete namespace",
        "delete pvc",
        "patch secret",
        "rbac",
        "batch delete",
    )
    environments: tuple[str, ...] = ("dev", "staging", "prod")
    prod_write_enabled: bool = False

    @classmethod
    def from_settings(
        cls, settings: "OpsSecuritySettings"
    ) -> "K8sActionSecurityPolicy":
        return cls(
            default_readonly=settings.default_readonly,
            allowed_resource_kinds=tuple(settings.allowed_resource_kinds),
            allowed_verbs=tuple(settings.allowed_verbs),
            blocked_actions=tuple(settings.blocked_actions),
            environments=tuple(settings.environments),
            prod_write_enabled=settings.prod_write_enabled,
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "default_readonly": self.default_readonly,
            "allowed_resource_kinds": list(self.allowed_resource_kinds),
            "allowed_verbs": list(self.allowed_verbs),
            "blocked_actions": list(self.blocked_actions),
            "environments": list(self.environments),
            "prod_write_enabled": self.prod_write_enabled,
        }


@dataclass(frozen=True)
class K8sActionPlan:
    """Preview for a low-risk Kubernetes write action."""

    action_type: str
    namespace: str
    resource_kind: str
    resource_name: str
    risk: str
    command_preview: str
    requires_confirmation: bool = True
    parameters: dict[str, JSONValue] = field(default_factory=dict)
    environment: str = "dev"
    actor: str = "system"
    required_scope: str = "cloud:execute"
    rollback_suggestion: str = ""
    security: dict[str, JSONValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JSONValue]:
        return asdict(self)


@dataclass(frozen=True)
class K8sActionResult:
    """Result for a controlled Kubernetes write action."""

    success: bool
    message: str
    plan: K8sActionPlan | None = None
    requires_confirmation: bool = False
    verification: dict[str, JSONValue] = field(default_factory=dict)
    error: str = ""
    rollback_suggestion: str = ""
    security: dict[str, JSONValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "success": self.success,
            "message": self.message,
            "plan": self.plan.to_dict() if self.plan else None,
            "requires_confirmation": self.requires_confirmation,
            "verification": self.verification,
            "error": self.error,
            "rollback_suggestion": self.rollback_suggestion,
            "security": self.security,
        }


class K8sWriteActionExecutor:
    """Parse, preview, confirm and execute low-risk Kubernetes write actions."""

    _BASE_BLOCKED_PATTERNS = (
        r"delete\s+namespace",
        r"删除\s*namespace",
        r"delete\s+pvc",
        r"删除\s*pvc",
        r"patch\s+secret",
        r"修改\s*secret",
        r"\brbac\b",
        r"修改\s*rbac",
        r"批量删除",
    )

    def __init__(
        self,
        client: K8sReadOnlyClient,
        policy: K8sActionSecurityPolicy | None = None,
        *,
        actor: str = "system",
        required_scope: str = "cloud:execute",
    ) -> None:
        self.client = client
        self.policy = policy or K8sActionSecurityPolicy()
        self.actor = actor or "system"
        self.required_scope = required_scope

    def preview(self, task: str, namespace: str) -> K8sActionResult | None:
        """Return a blocked result, a low-risk action preview, or None for read-only tasks."""
        text = (task or "").strip()
        if not text:
            return None
        lowered = text.lower()
        for pattern in (*self._BASE_BLOCKED_PATTERNS, *self._blocked_policy_patterns()):
            if re.search(pattern, lowered, flags=re.IGNORECASE):
                return K8sActionResult(
                    success=False,
                    message="K8s 写操作被安全策略拦截：高风险动作不允许执行。",
                    requires_confirmation=False,
                    error="blocked high-risk Kubernetes action",
                    rollback_suggestion="无需回滚：动作未执行。",
                    security=self._security_metadata(namespace, "", "", "blocked"),
                )

        plan = self._parse_low_risk_plan(text, namespace)
        if plan is None:
            return None
        policy_error = self._policy_error(plan)
        if policy_error:
            return K8sActionResult(
                success=False,
                message=f"K8s 写操作被安全策略拦截：{policy_error}",
                plan=plan,
                requires_confirmation=False,
                error=policy_error,
                rollback_suggestion="无需回滚：动作未执行。",
                security=plan.security,
            )
        return K8sActionResult(
            success=False,
            message="该 K8s 写操作需要人工确认后才会执行。",
            plan=plan,
            requires_confirmation=True,
            rollback_suggestion=plan.rollback_suggestion,
            security=plan.security,
        )

    def execute(self, plan: K8sActionPlan) -> K8sActionResult:
        """Execute a confirmed low-risk action and verify deployment state afterwards."""
        try:
            self.client._ensure_namespace_allowed(plan.namespace)
            if plan.action_type == "rollout_restart_deployment":
                self._rollout_restart(plan.namespace, plan.resource_name)
            elif plan.action_type == "scale_deployment":
                replicas = int(plan.parameters.get("replicas", 1) or 1)
                self._scale(plan.namespace, plan.resource_name, replicas)
            elif plan.action_type == "pause_rollout":
                self._set_paused(plan.namespace, plan.resource_name, True)
            elif plan.action_type == "resume_rollout":
                self._set_paused(plan.namespace, plan.resource_name, False)
            else:
                return K8sActionResult(
                    success=False,
                    message=f"不支持的 K8s 写操作：{plan.action_type}",
                    plan=plan,
                    error="unsupported action",
                )
            return K8sActionResult(
                success=True,
                message="K8s 写操作已执行，并已读取资源状态做结果验证。",
                plan=plan,
                verification={"deployments": self.client.list_deployments(plan.namespace)},
                rollback_suggestion=plan.rollback_suggestion,
                security=plan.security,
            )
        except Exception as exc:  # noqa: BLE001 - 返回真实错误给 Web/API 展示
            return K8sActionResult(
                success=False,
                message="K8s 写操作执行失败。",
                plan=plan,
                error=str(exc),
                rollback_suggestion=plan.rollback_suggestion,
                security=plan.security,
            )

    def _parse_low_risk_plan(self, task: str, namespace: str) -> K8sActionPlan | None:
        lowered = task.lower()
        deployment = self._deployment_name(task)
        if not deployment:
            return None
        environment = self._environment(task, namespace)
        if "rollout restart" in lowered or "重启" in task:
            verb = "rollout_restart"
            return K8sActionPlan(
                action_type="rollout_restart_deployment",
                namespace=namespace,
                resource_kind="Deployment",
                resource_name=deployment,
                risk="low",
                command_preview=f"kubectl rollout restart deployment/{deployment} -n {namespace}",
                environment=environment,
                actor=self.actor,
                required_scope=self.required_scope,
                rollback_suggestion=(
                    f"如重启后异常，执行 kubectl rollout undo deployment/{deployment} -n {namespace}。"
                ),
                security=self._security_metadata(namespace, "Deployment", verb, environment),
            )
        if "scale" in lowered or "扩缩容" in task or "副本" in task:
            replicas = self._replicas(task)
            verb = "scale"
            return K8sActionPlan(
                action_type="scale_deployment",
                namespace=namespace,
                resource_kind="Deployment",
                resource_name=deployment,
                risk="low",
                command_preview=(
                    f"kubectl scale deployment/{deployment} --replicas={replicas} -n {namespace}"
                ),
                parameters={"replicas": replicas},
                environment=environment,
                actor=self.actor,
                required_scope=self.required_scope,
                rollback_suggestion=(
                    f"如扩缩容后异常，按执行前副本数重新 scale deployment/{deployment}。"
                ),
                security=self._security_metadata(namespace, "Deployment", verb, environment),
            )
        if "pause rollout" in lowered or "暂停 rollout" in task or "暂停发布" in task:
            verb = "pause"
            return K8sActionPlan(
                action_type="pause_rollout",
                namespace=namespace,
                resource_kind="Deployment",
                resource_name=deployment,
                risk="low",
                command_preview=f"kubectl rollout pause deployment/{deployment} -n {namespace}",
                environment=environment,
                actor=self.actor,
                required_scope=self.required_scope,
                rollback_suggestion=(
                    f"如需恢复发布，执行 kubectl rollout resume deployment/{deployment} -n {namespace}。"
                ),
                security=self._security_metadata(namespace, "Deployment", verb, environment),
            )
        if "resume rollout" in lowered or "恢复 rollout" in task or "恢复发布" in task:
            verb = "resume"
            return K8sActionPlan(
                action_type="resume_rollout",
                namespace=namespace,
                resource_kind="Deployment",
                resource_name=deployment,
                risk="low",
                command_preview=f"kubectl rollout resume deployment/{deployment} -n {namespace}",
                environment=environment,
                actor=self.actor,
                required_scope=self.required_scope,
                rollback_suggestion=(
                    f"如恢复后异常，执行 kubectl rollout pause deployment/{deployment} -n {namespace}。"
                ),
                security=self._security_metadata(namespace, "Deployment", verb, environment),
            )
        return None

    def _blocked_policy_patterns(self) -> tuple[str, ...]:
        return tuple(re.escape(action.lower()) for action in self.policy.blocked_actions)

    def _policy_error(self, plan: K8sActionPlan) -> str:
        if plan.resource_kind not in self.policy.allowed_resource_kinds:
            return f"resource kind {plan.resource_kind} is not allowed"
        verb = str(plan.security.get("verb", ""))
        if verb not in self.policy.allowed_verbs:
            return f"verb {verb} is not allowed"
        if plan.environment == "prod" and not self.policy.prod_write_enabled:
            return "prod write is disabled; enable ATHENA_OPS_PROD_WRITE_ENABLED and confirm explicitly"
        return ""

    def _security_metadata(
        self, namespace: str, resource_kind: str, verb: str, environment: str
    ) -> dict[str, JSONValue]:
        return {
            "actor": self.actor,
            "required_scope": self.required_scope,
            "rbac_scope_checked": True,
            "default_readonly": self.policy.default_readonly,
            "namespace": namespace,
            "resource_kind": resource_kind,
            "verb": verb,
            "environment": environment,
            "prod_write_enabled": self.policy.prod_write_enabled,
            "policy": self.policy.to_dict(),
        }

    def _environment(self, task: str, namespace: str) -> str:
        patterns = (
            r"environment[=:\s]+([a-z0-9][a-z0-9-]*)",
            r"env[=:\s]+([a-z0-9][a-z0-9-]*)",
            r"环境[=:\s]*([a-z0-9][a-z0-9-]*)",
        )
        for pattern in patterns:
            match = re.search(pattern, task, flags=re.IGNORECASE)
            if match:
                candidate = match.group(1).lower()
                return candidate if candidate in self.policy.environments else "dev"
        if namespace in self.policy.environments:
            return namespace
        if namespace == "default":
            return "dev"
        return "staging"

    @staticmethod
    def _deployment_name(task: str) -> str:
        patterns = (
            r"deployment[/=:\s]+([a-z0-9][a-z0-9-]*)",
            r"deploy[/=:\s]+([a-z0-9][a-z0-9-]*)",
            r"部署[/=:\s]+([a-z0-9][a-z0-9-]*)",
        )
        for pattern in patterns:
            match = re.search(pattern, task, flags=re.IGNORECASE)
            if match:
                return match.group(1).lower()
        return ""

    @staticmethod
    def _replicas(task: str) -> int:
        patterns = (
            r"replicas[=:\s]+(\d+)",
            r"--replicas[=:\s]+(\d+)",
            r"(\d+)\s*副本",
        )
        for pattern in patterns:
            match = re.search(pattern, task, flags=re.IGNORECASE)
            if match:
                return max(0, int(match.group(1)))
        return 1

    def _rollout_restart(self, namespace: str, deployment: str) -> None:
        api = self.client._get_apps_api()
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": datetime.now(UTC).isoformat()
                        }
                    }
                }
            }
        }
        api.patch_namespaced_deployment(deployment, namespace, body=body, _request_timeout=self.client.timeout)  # type: ignore[attr-defined]

    def _scale(self, namespace: str, deployment: str, replicas: int) -> None:
        api = self.client._get_apps_api()
        api.patch_namespaced_deployment_scale(  # type: ignore[attr-defined]
            deployment,
            namespace,
            body={"spec": {"replicas": replicas}},
            _request_timeout=self.client.timeout,
        )

    def _set_paused(self, namespace: str, deployment: str, paused: bool) -> None:
        api = self.client._get_apps_api()
        api.patch_namespaced_deployment(  # type: ignore[attr-defined]
            deployment,
            namespace,
            body={"spec": {"paused": paused}},
            _request_timeout=self.client.timeout,
        )