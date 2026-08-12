"""Unified HTTP API layer for Athena Agent.

The application factory is loaded lazily so repository and application
modules do not import the complete web/observability stack as a side effect.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from athena.api.server import create_app


def __getattr__(name: str) -> object:
    if name == "create_app":
        from athena.api.server import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["create_app"]
