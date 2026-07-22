from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from camouflare._version import __version__
from camouflare.errors import V1ErrorCode
from camouflare.limits import (
    MAX_COOKIE_BYTES,
    MAX_COOKIES,
    MAX_SESSION_ID_LENGTH,
    MAX_TARGET_HEADER_BYTES,
    MAX_TARGET_HEADERS,
    MAX_URL_LENGTH,
    json_size,
)


class V1Request(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    cmd: str | None = Field(
        default=None,
        description=(
            "Command to run. Supported values are sessions.create, sessions.list, "
            "sessions.destroy, request.get, and request.post."
        ),
        examples=["request.get"],
    )
    url: str | None = Field(
        default=None,
        max_length=MAX_URL_LENGTH,
        description=(
            "Target URL for request.get and request.post. Double quotes are stripped "
            "before navigation for FlareSolverr compatibility."
        ),
        examples=["https://example.com"],
    )
    max_timeout: int = Field(
        default=60000,
        gt=0,
        description="Maximum command runtime in milliseconds.",
        examples=[60000],
        validation_alias=AliasChoices("maxTimeout", "max_timeout"),
    )
    proxy: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Request-level proxy. Accepts url or server, plus optional username and password."
        ),
        examples=[
            {
                "url": "http://proxy.example:8080",
                "username": "user",
                "password": "pass",
            }
        ],
    )
    session: str | None = Field(
        default=None,
        max_length=MAX_SESSION_ID_LENGTH,
        description=(
            "Persistent session id. Requests with the same session reuse browser state and cookies."
        ),
        examples=["account-a"],
    )
    session_ttl_minutes: int | None = Field(
        default=None,
        gt=0,
        description="Override the default TTL for a created or rotated session.",
        examples=[240],
    )
    cookies: list[dict[str, Any]] | None = Field(
        default=None,
        max_length=MAX_COOKIES,
        description="Cookies to inject into the browser context before navigation.",
        examples=[
            [
                {
                    "name": "session",
                    "value": "abc",
                    "domain": "example.com",
                    "path": "/",
                }
            ]
        ],
    )
    return_only_cookies: bool = Field(
        default=False,
        description=(
            "When true, the solution includes cookies and omits response HTML, "
            "headers, and screenshots."
        ),
        examples=[False],
        validation_alias=AliasChoices("returnOnlyCookies", "return_only_cookies"),
    )
    return_screenshot: bool = Field(
        default=False,
        description="When true, include a base64-encoded PNG screenshot in solution.screenshot.",
        examples=[False],
        validation_alias=AliasChoices("returnScreenshot", "return_screenshot"),
    )
    wait_in_seconds: int | None = Field(
        default=None,
        ge=0,
        description="Optional post-load wait before collecting cookies, HTML, and screenshot.",
        examples=[3],
        validation_alias=AliasChoices("waitInSeconds", "wait_in_seconds"),
    )
    disable_media: bool | None = Field(
        default=None,
        description="When true, block image, font, stylesheet, and icon resources.",
        examples=[True],
        validation_alias=AliasChoices("disableMedia", "disable_media"),
    )
    post_data: str | None = Field(
        default=None,
        description=(
            "Request body for request.post. Defaults to URL-encoded form data, "
            "for example username=alice&password=secret. When Content-Type is "
            "application/json or another +json media type, the value is sent as "
            "the raw JSON body."
        ),
        examples=["username=alice&password=secret"],
        validation_alias=AliasChoices("postData", "post_data"),
    )
    # Accepted for FlareSolverr compatibility but not all are actionable.
    tabs_till_verify: int | None = Field(
        default=None,
        description="Accepted for FlareSolverr compatibility; currently ignored.",
    )
    headers: dict[str, Any] | None = Field(
        default=None,
        max_length=MAX_TARGET_HEADERS,
        description=(
            "HTTP headers to apply to the target page request. Header names and "
            "values are coerced to strings."
        ),
    )
    user_agent: str | None = Field(
        default=None,
        description=(
            "Browser User-Agent override for new contexts. Also takes precedence "
            "over any User-Agent value supplied in headers."
        ),
        validation_alias=AliasChoices("userAgent", "user_agent"),
    )
    download: bool | None = Field(
        default=None,
        description="Accepted for FlareSolverr compatibility; currently ignored.",
    )
    return_raw_html: bool | None = Field(
        default=None,
        description="Accepted for FlareSolverr compatibility; currently ignored.",
        validation_alias=AliasChoices("returnRawHtml", "return_raw_html"),
    )

    @model_validator(mode="after")
    def validate_structural_sizes(self) -> V1Request:
        if self.headers is not None and json_size(self.headers) > MAX_TARGET_HEADER_BYTES:
            raise ValueError(
                f"Request parameter 'headers' exceeds the {MAX_TARGET_HEADER_BYTES}-byte limit."
            )
        if self.cookies is not None and json_size(self.cookies) > MAX_COOKIE_BYTES:
            raise ValueError(
                f"Request parameter 'cookies' exceeds the {MAX_COOKIE_BYTES}-byte limit."
            )
        return self

    def target_headers(self) -> dict[str, str]:
        if self.headers is None:
            headers: dict[str, str] = {}
        elif isinstance(self.headers, Mapping):
            headers = {
                str(name): str(value)
                for name, value in self.headers.items()
                if value is not None
                and str(name).lower() not in {"referer", "referrer", "user-agent"}
            }
        else:
            raise RuntimeError("Request parameter 'headers' must be an object.")

        return headers

    def target_referer(self) -> str | None:
        if self.headers is None:
            return None
        if not isinstance(self.headers, Mapping):
            raise RuntimeError("Request parameter 'headers' must be an object.")

        for name, value in self.headers.items():
            if str(name).lower() in {"referer", "referrer"} and value is not None:
                return str(value)
        return None

    def target_user_agent(self) -> str | None:
        if self.user_agent:
            return self.user_agent
        if self.headers is None:
            return None
        if not isinstance(self.headers, Mapping):
            raise RuntimeError("Request parameter 'headers' must be an object.")

        for name, value in self.headers.items():
            if str(name).lower() == "user-agent" and value is not None:
                return str(value)
        return None


class Solution(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    url: str = Field(description="Final page URL after navigation and redirects.")
    status: int = Field(description="HTTP status code from the page response.")
    headers: dict[str, Any] | None = Field(
        default=None,
        description="Response headers when returnOnlyCookies is false.",
    )
    response: str | None = Field(
        default=None,
        description="HTML response body when returnOnlyCookies is false.",
    )
    cookies: list[dict[str, Any]] = Field(description="Cookies collected from the context.")
    user_agent: str = Field(
        default="",
        description="Browser navigator.userAgent value.",
        serialization_alias="userAgent",
    )
    screenshot: str | None = Field(
        default=None,
        description="Base64-encoded PNG when returnScreenshot is true.",
    )
    turnstile_token: str | None = Field(
        default=None,
        description="Captcha provider token when a Turnstile solve returns one.",
    )


class V1Response(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="ok", description='Envelope status: "ok" or "error".')
    message: str = Field(default="", description="Human-readable result or error message.")
    solution: Solution | None = Field(
        default=None,
        description="Browser solution payload for request.get and request.post commands.",
    )
    session: str | None = Field(
        default=None,
        description="Created or reused session id for sessions.create.",
    )
    sessions: list[str] | None = Field(
        default=None,
        description="Sorted session ids for sessions.list.",
    )
    error_code: V1ErrorCode | None = Field(
        default=None,
        description="Stable machine-readable error category.",
        serialization_alias="errorCode",
    )
    retryable: bool | None = Field(
        default=None,
        description="Whether retrying the command may succeed.",
    )
    request_outcome_unknown: bool | None = Field(
        default=None,
        description="Whether a failed request may have reached the target.",
        serialization_alias="requestOutcomeUnknown",
    )
    fallback_used: bool | None = Field(
        default=None,
        description="Whether browser navigation fell back to direct HTTP.",
        serialization_alias="fallbackUsed",
    )
    start_timestamp: int = Field(
        default_factory=lambda: int(time.time() * 1000),
        description="Unix timestamp in milliseconds when the command started.",
        serialization_alias="startTimestamp",
    )
    end_timestamp: int = Field(
        default_factory=lambda: int(time.time() * 1000),
        description="Unix timestamp in milliseconds when the command finished.",
        serialization_alias="endTimestamp",
    )
    version: str = Field(default=__version__, description="Camouflare response version.")

    @classmethod
    def error(
        cls,
        message: str,
        *,
        version: str,
        start_timestamp: int | None = None,
        error_code: V1ErrorCode | None = None,
        retryable: bool | None = None,
        request_outcome_unknown: bool | None = None,
        fallback_used: bool | None = None,
        solution: Solution | None = None,
    ) -> V1Response:
        now = int(time.time() * 1000)
        return cls(
            status="error",
            message=message,
            error_code=error_code,
            retryable=retryable,
            request_outcome_unknown=request_outcome_unknown,
            fallback_used=fallback_used,
            solution=solution,
            start_timestamp=start_timestamp or now,
            end_timestamp=now,
            version=version,
        )


class IndexResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    msg: str = Field(default="Camouflare is ready!", description="Service readiness message.")
    version: str = Field(description="Configured Camouflare version.")
    user_agent: str = Field(
        default="",
        description="Reserved for FlareSolverr compatibility.",
        serialization_alias="userAgent",
    )


class HealthResponse(BaseModel):
    status: str = Field(default="ok", description="Health status.")


class DiagnosticsPoolStatus(BaseModel):
    ready_browser_slots: int = Field(ge=0)
    retiring_browser_slots: int = Field(ge=0)
    creating_slots: int = Field(ge=0)
    closing_slots: int = Field(ge=0)
    active_contexts: int = Field(ge=0)
    transient_contexts: int = Field(ge=0)
    persistent_contexts: int = Field(ge=0)
    waiting_requests: int = Field(ge=0)
    usable_context_slots: int = Field(ge=0)
    idle_recyclable_slots: int = Field(ge=0)
    max_browsers: int = Field(ge=1)
    max_contexts_per_browser: int = Field(ge=1)
    max_slots: int = Field(ge=1)


class DiagnosticsSessionStatus(BaseModel):
    active: int = Field(ge=0)
    in_use: int = Field(ge=0)
    closing: int = Field(ge=0)
    max_sessions: int = Field(ge=1)


class DiagnosticsCleanupStatus(BaseModel):
    in_flight: int = Field(ge=0)
    oldest_age_seconds: float | None = Field(default=None, ge=0)
    by_kind: dict[str, int] = Field(default_factory=dict)


class DiagnosticsRuntimeStatus(BaseModel):
    playwright_version: str
    playwright_cancel_patch: str


class DiagnosticsResponse(BaseModel):
    status: str = "ok"
    capacity_state: Literal["available", "saturated", "recovering", "unavailable"]
    pool: DiagnosticsPoolStatus
    sessions: DiagnosticsSessionStatus
    cleanup: DiagnosticsCleanupStatus
    runtime: DiagnosticsRuntimeStatus
