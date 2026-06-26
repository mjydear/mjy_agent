"""
📦 模块名称：工具权限管理（Tool Permissions）
📍 架构位置：工具执行层的安全网关，位于 ToolExecutor 调用真实工具之前。
🎯 核心作用：按工具名、风险级别、目录、网络主机和人工确认控制工具是否允许执行。
🔗 依赖关系：只依赖 pathlib 和 dataclass；可被 ToolExecutor、企业沙箱、CI/CD 集成调用。
💡 设计思路：使用“策略对象 + 守卫检查”模式。ToolPermission 描述规则，PermissionManager 执行规则。
📚 学习重点：理解 Agent 安全不能只靠 Prompt，还要在代码层做强制权限检查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ToolPermission:
    """
    单个工具的权限策略。

    功能说明：描述某个工具允许做什么、不允许做什么。
    参数说明：
        tool_name：工具名。
        risk_level：风险级别，可选 read、write、high。
        allowed_directories：允许访问的目录白名单。
        allowed_hosts：允许访问的网络主机白名单。
        requires_confirmation：是否需要人工确认。
    返回值：数据容器。
    设计思路：把权限规则做成数据对象，方便配置文件、管理后台或测试统一创建。
    使用示例：ToolPermission("delete", risk_level="high", requires_confirmation=True)

    🎯 面试考点：为什么要 requires_confirmation？答案：高危操作应该有人类确认，避免 Agent 自动执行破坏性动作。
    """

    tool_name: str
    risk_level: str = "read"
    allowed_directories: tuple[Path, ...] = field(default_factory=tuple)
    allowed_hosts: tuple[str, ...] = field(default_factory=tuple)
    requires_confirmation: bool = False

    def __post_init__(self) -> None:
        if not self.tool_name.strip():
            raise ValueError("tool_name must be non-empty")
        if self.risk_level not in {"read", "write", "high"}:
            raise ValueError("risk_level must be read, write or high")


class PermissionManager:
    """
    细粒度权限管理器。

    功能说明：保存工具权限策略，并在工具调用前做统一校验。
    参数说明：permissions 是初始权限策略集合。
    返回值：assert_allowed() 成功时返回 None，失败时抛 PermissionError。
    设计思路：集中式守卫比每个工具自己检查更可靠，避免遗漏。
    使用示例：manager.assert_allowed("read", target_path=Path("logs/app.log"))
    """

    def __init__(self, permissions: tuple[ToolPermission, ...] = ()) -> None:
        self.permissions = {
            permission.tool_name: permission for permission in permissions
        }

    def register(self, permission: ToolPermission) -> None:
        """
        注册或覆盖一个工具权限。

        功能说明：把 ToolPermission 放入管理器。
        参数说明：permission 是权限策略对象。
        返回值：None。
        设计思路：允许覆盖，方便运行时加载新策略或测试替换策略。
        使用示例：manager.register(ToolPermission("git_status"))
        """
        if not isinstance(permission, ToolPermission):
            raise ValueError("permission must be a ToolPermission")
        self.permissions[permission.tool_name] = permission

    def assert_allowed(
        self,
        tool_name: str,
        target_path: Path | None = None,
        host: str | None = None,
        confirmed: bool = False,
    ) -> None:
        """
        校验工具调用是否满足权限策略。

        功能说明：检查工具是否有策略、是否需要确认、路径和主机是否在白名单内。
        参数说明：
            tool_name：要调用的工具名。
            target_path：工具要访问的本地路径。
            host：工具要访问的网络主机。
            confirmed：是否已经获得人工确认。
        返回值：None；如果不允许会抛 PermissionError。
        设计思路：失败即抛异常，让上层停止执行，不给危险操作留下继续运行的机会。
        使用示例：manager.assert_allowed("delete", confirmed=True)

        🔍 原理讲解：
        输入：工具名 + 目标路径 + 主机 + 是否确认。
        处理过程：查策略 → 检查确认 → resolve 路径 → 检查目录白名单 → 检查 host 白名单。
        输出：允许则静默返回，不允许则抛 PermissionError。
        """
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise ValueError("tool_name must be a non-empty string")
        permission = self.permissions.get(tool_name)
        if permission is None:
            raise PermissionError(f"tool has no permission policy: {tool_name}")
        if permission.requires_confirmation and not confirmed:
            raise PermissionError("tool requires human confirmation")
        if target_path is not None and permission.allowed_directories:
            resolved = (
                target_path.resolve()
            )  # 💡 学习提示：resolve 能处理 ../ 这类路径逃逸，避免只用字符串前缀判断造成安全漏洞。
            if not any(
                directory.resolve() == resolved
                or directory.resolve() in resolved.parents
                for directory in permission.allowed_directories
            ):
                raise PermissionError("target path is outside allowed directories")
        if (
            host is not None
            and permission.allowed_hosts
            and host not in permission.allowed_hosts
        ):
            raise PermissionError("host is not allowed")


"""
🤔 思考题：

1. 如果 allowed_directories 为空，当前逻辑表示不限制路径；生产环境是否应该默认拒绝？
2. 为什么路径检查要用 Path.resolve()，而不是简单 startswith 字符串？
3. 如果工具同时访问多个文件，assert_allowed() 应该如何扩展？
4. ⚡ 优化建议：未来可以增加 denylist、操作类型 read/write/delete，以及审计日志联动。
"""
