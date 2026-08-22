"""Stable errors shared by HTTP adapters."""

from __future__ import annotations


class ApiServiceError(Exception):
    """A safe, machine-readable error exposed by an API boundary."""

    def __init__(self, error_code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status_code = status_code


__all__ = ["ApiServiceError"]
