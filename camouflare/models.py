from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping
from ipaddress import ip_address
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from camouflare._version import __version__
from camouflare.config import normalize_proxy
from camouflare.cookie_policy import is_public_suffix
from camouflare.errors import CamouflareError, V1ErrorCode
from camouflare.limits import (
    MAX_COOKIE_BYTES,
    MAX_COOKIES,
    MAX_SESSION_ID_LENGTH,
    MAX_TARGET_HEADER_BYTES,
    MAX_TARGET_HEADERS,
    MAX_URL_LENGTH,
    json_size,
)

_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
# Browsers store any printable ASCII value except the pair separator, which is
# looser than the RFC 6265 cookie-octet grammar. Cookies that Camouflare itself
# collected must round-trip, so accept what the browser accepts.
_COOKIE_VALUE = re.compile(r"^[\x20-\x3A\x3C-\x7E]*$")
_HTTP_FIELD_NAME = _COOKIE_NAME
_COOKIE_SAME_SITE_VALUES = {"strict": "Strict", "lax": "Lax", "none": "None"}
# Unknown fields (Puppeteer's ``session``/``priority``, Chrome exports'
# ``hostOnly``/``storeId``, ...) are dropped like every other unknown request
# field; only the fields below reach the browser.
_COOKIE_FIELDS = frozenset(
    {
        "name",
        "value",
        "url",
        "domain",
        "path",
        "expires",
        "expiry",
        "httpOnly",
        "secure",
        "sameSite",
        "partitionKey",
    }
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
            "Origin-bound target headers. Non-User-Agent headers make request.get use "
            "stateless direct HTTP without a proxy and make request.post use the browser "
            "context request transport with redirects disabled. Header names and values "
            "are coerced to strings. User-Agent is the exception: it configures browser "
            "identity for the whole context."
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

    @field_validator("url")
    @classmethod
    def validate_target_url(cls, value: str | None) -> str | None:
        if value:
            _validate_absolute_http_url(value.replace('"', "").strip(), parameter="url")
        return value

    @field_validator("proxy")
    @classmethod
    def validate_proxy(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None:
            try:
                normalized = normalize_proxy(value)
            except CamouflareError as exc:
                raise ValueError(str(exc)) from exc
            if normalized is None:
                raise ValueError("Request parameter 'proxy' must include a server.")
        return value

    @field_validator("headers")
    @classmethod
    def validate_target_headers(
        cls,
        value: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        for name, raw_value in value.items():
            if not _HTTP_FIELD_NAME.fullmatch(name):
                raise ValueError(f"Request parameter 'headers' contains invalid name '{name}'.")
            if raw_value is not None and _has_forbidden_header_control(str(raw_value)):
                raise ValueError(
                    f"Request parameter 'headers[{name}]' contains invalid control characters."
                )
        return value

    @field_validator("user_agent")
    @classmethod
    def validate_user_agent(cls, value: str | None) -> str | None:
        if value is not None and _has_forbidden_header_control(value):
            raise ValueError("Request parameter 'userAgent' contains invalid control characters.")
        return value

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
        if self.cookies is not None:
            normalized_cookies: list[dict[str, Any]] = []
            for index, cookie in enumerate(self.cookies):
                normalized_cookies.append(
                    _validate_cookie(cookie, index=index, target_url=self.url)
                )
            if json_size(normalized_cookies) > MAX_COOKIE_BYTES:
                raise ValueError(
                    f"Request parameter 'cookies' exceeds the {MAX_COOKIE_BYTES}-byte limit "
                    "after applying target URL cookie scope."
                )
            self.cookies = normalized_cookies
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


def _validate_absolute_http_url(value: str, *, parameter: str) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(
            f"Request parameter '{parameter}' must be a valid absolute http or https URL."
        )
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(
            f"Request parameter '{parameter}' must be a valid absolute http or https URL."
        ) from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or hostname is None
        or any(character.isspace() for character in hostname)
    ):
        raise ValueError(
            f"Request parameter '{parameter}' must be a valid absolute http or https URL."
        )


def _has_forbidden_header_control(value: str) -> bool:
    return any(
        (ord(character) < 32 and character != "\t") or ord(character) == 127 for character in value
    )


def _validate_cookie(
    cookie: dict[str, Any],
    *,
    index: int,
    target_url: str | None,
) -> dict[str, Any]:
    name = cookie.get("name")
    value = cookie.get("value")
    if not isinstance(name, str) or not _COOKIE_NAME.fullmatch(name):
        raise ValueError(f"Request parameter 'cookies[{index}].name' is invalid.")
    if not isinstance(value, str) or not _COOKIE_VALUE.fullmatch(value):
        raise ValueError(f"Request parameter 'cookies[{index}].value' is invalid.")

    normalized_input = {
        field_name: field_value
        for field_name, field_value in cookie.items()
        if field_name in _COOKIE_FIELDS
    }
    if "expiry" in normalized_input:
        if (
            "expires" in normalized_input
            and normalized_input["expires"] != normalized_input["expiry"]
        ):
            raise ValueError(
                f"Request parameter 'cookies[{index}]' cannot contain conflicting "
                "'expiry' and 'expires' values."
            )
        normalized_input["expires"] = normalized_input.pop("expiry")
    if "sameSite" in normalized_input:
        same_site = normalized_input["sameSite"]
        canonical_same_site = (
            _COOKIE_SAME_SITE_VALUES.get(same_site.casefold())
            if isinstance(same_site, str)
            else None
        )
        if canonical_same_site is None:
            raise ValueError(f"Request parameter 'cookies[{index}].sameSite' is invalid.")
        normalized_input["sameSite"] = canonical_same_site

    cookie_url = normalized_input.get("url")
    domain = normalized_input.get("domain")
    if cookie_url is not None and domain is not None:
        raise ValueError(
            f"Request parameter 'cookies[{index}]' cannot include both 'url' and 'domain'."
        )
    if cookie_url is not None:
        if not isinstance(cookie_url, str):
            raise ValueError(f"Request parameter 'cookies[{index}].url' is invalid.")
        _validate_absolute_http_url(cookie_url, parameter=f"cookies[{index}].url")
    elif domain is not None:
        if not isinstance(domain, str) or not _valid_cookie_domain(domain):
            raise ValueError(f"Request parameter 'cookies[{index}].domain' is invalid.")
        if is_public_suffix(domain):
            raise ValueError(
                f"Request parameter 'cookies[{index}].domain' cannot be a public suffix."
            )
    elif target_url:
        cookie_url = target_url.replace('"', "").strip()
        normalized_input["url"] = cookie_url
    else:
        raise ValueError(
            f"Request parameter 'cookies[{index}]' must include either 'url' or 'domain' "
            "when no target URL is available."
        )

    normalized = {key: value for key, value in normalized_input.items() if value is not None}
    if domain is not None or "path" in normalized_input:
        path = normalized_input.get("path") if "path" in normalized_input else "/"
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError(f"Request parameter 'cookies[{index}].path' is invalid.")
        if any(ord(character) < 32 or ord(character) == 127 for character in path):
            raise ValueError(f"Request parameter 'cookies[{index}].path' is invalid.")
        normalized["path"] = path
    for field_name in ("secure", "httpOnly"):
        if field_name in normalized_input and not isinstance(normalized_input[field_name], bool):
            raise ValueError(f"Request parameter 'cookies[{index}].{field_name}' is invalid.")
    if "expires" in normalized_input:
        expires = normalized_input["expires"]
        if (
            isinstance(expires, bool)
            or not isinstance(expires, (int, float))
            or not math.isfinite(float(expires))
        ):
            raise ValueError(f"Request parameter 'cookies[{index}].expires' is invalid.")
    if "partitionKey" in normalized_input:
        partition_key = normalized_input["partitionKey"]
        if not isinstance(partition_key, str):
            raise ValueError(f"Request parameter 'cookies[{index}].partitionKey' is invalid.")
        _validate_absolute_http_url(
            partition_key,
            parameter=f"cookies[{index}].partitionKey",
        )
        if not normalized_input.get("secure"):
            raise ValueError(
                f"Request parameter 'cookies[{index}].partitionKey' requires 'secure' to be true."
            )
    if normalized_input.get("sameSite") == "None" and not normalized_input.get("secure"):
        raise ValueError(
            f"Request parameter 'cookies[{index}].sameSite' = 'None' requires 'secure' to be true."
        )
    if name.startswith("__Secure-") and not normalized_input.get("secure"):
        raise ValueError(
            f"Request parameter 'cookies[{index}]' uses the __Secure- prefix without 'secure'."
        )
    if name.startswith("__Host-") and (
        not normalized_input.get("secure")
        or cookie_url is None
        or normalized.get("path", "/") != "/"
    ):
        raise ValueError(f"Request parameter 'cookies[{index}]' has invalid __Host- cookie scope.")
    return normalized


def _valid_cookie_domain(domain: str) -> bool:
    if (
        not domain
        or domain.startswith("..")
        or domain.endswith(".")
        or any(character.isspace() for character in domain)
        or any(character in domain for character in "/@?#:")
    ):
        return False
    host = domain.lstrip(".")
    try:
        ip_address(host)
        return True
    except ValueError:
        pass
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if len(ascii_host) > 253:
        return False
    return all(
        0 < len(label) <= 63
        and label[0].isalnum()
        and label[-1].isalnum()
        and all(character.isalnum() or character == "-" for character in label)
        for label in ascii_host.split(".")
    )


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


class ReadyResponse(HealthResponse):
    capacity_state: Literal["saturated"] | None = Field(
        default=None,
        description=(
            "Present only when every context slot is held by a browser that is still "
            "serving, so the browser probe was skipped."
        ),
    )
    message: str | None = Field(default=None, description="Why the probe was skipped.")


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
