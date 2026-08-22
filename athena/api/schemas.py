"""Small HTTP error contract shared by Runtime routes."""

from __future__ import annotations

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    error_code: str
    message: str
    trace_id: str = ""


__all__ = ["ErrorResponse"]
