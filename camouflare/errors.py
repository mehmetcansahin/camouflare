from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from camouflare.models import Solution


class V1ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    RESOURCE_LIMIT_EXCEEDED = "RESOURCE_LIMIT_EXCEEDED"
    POOL_UNAVAILABLE = "POOL_UNAVAILABLE"
    REQUEST_TIMEOUT = "REQUEST_TIMEOUT"
    NAVIGATION_TIMEOUT = "NAVIGATION_TIMEOUT"
    BROWSER_TRANSPORT_CLOSED = "BROWSER_TRANSPORT_CLOSED"
    CHALLENGE_FAILED = "CHALLENGE_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class CamouflareError(Exception):
    def __init__(
        self,
        message: str,
        *,
        error_code: V1ErrorCode,
        retryable: bool = False,
        request_outcome_unknown: bool = False,
        fallback_used: bool | None = None,
        solution: Solution | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.request_outcome_unknown = request_outcome_unknown
        self.fallback_used = fallback_used
        self.solution = solution
