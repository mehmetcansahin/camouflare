from __future__ import annotations

import logging
import os
import platform
from dataclasses import dataclass, field
from ipaddress import ip_address
from typing import Any
from urllib.parse import SplitResult, unquote, urlsplit, urlunsplit

from camouflare._version import __version__
from camouflare.errors import CamouflareError, V1ErrorCode
from camouflare.limits import ResourceLimits

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r; using default %d.", name, value, default)
        return default


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _headless_env() -> str | bool:
    value = os.getenv("HEADLESS")
    if value is None or value == "":
        return "virtual" if platform.system() == "Linux" else True

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    if normalized == "virtual":
        return "virtual"
    raise ValueError("HEADLESS must be one of true, false, 1, 0, yes, no, on, off, or virtual.")


def _challenge_solver_env() -> str:
    value = os.getenv("CHALLENGE_SOLVER")
    if value is None or value == "":
        return "none"
    normalized = value.strip().lower()
    if normalized in {"click", "none"}:
        return normalized
    raise ValueError("CHALLENGE_SOLVER must be one of click or none.")


@dataclass(frozen=True)
class Settings:
    # Read env at instantiation (not import) via default_factory, so the values are
    # consistent with `headless` and reflect the environment when Settings() is built.
    host: str = field(default_factory=lambda: os.getenv("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int_env("PORT", 8191))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())
    # The package version is intentionally not configurable: API responses, package
    # metadata, and the CLI must all report the same release identity.
    version: str = field(default=__version__, init=False)
    camouflare_api_token: str | None = field(
        default_factory=lambda: os.getenv("CAMOUFLARE_API_TOKEN")
    )
    headless: str | bool = field(default_factory=_headless_env)
    proxy_url: str | None = field(
        default_factory=lambda: os.getenv("PROXY_URL") or os.getenv("PROXY_SERVER")
    )
    proxy_username: str | None = field(default_factory=lambda: os.getenv("PROXY_USERNAME"))
    proxy_password: str | None = field(default_factory=lambda: os.getenv("PROXY_PASSWORD"))
    pool_min_browsers: int = field(default_factory=lambda: _int_env("POOL_MIN_BROWSERS", 1))
    pool_max_browsers: int = field(default_factory=lambda: _int_env("POOL_MAX_BROWSERS", 2))
    pool_max_contexts_per_browser: int = field(
        default_factory=lambda: _int_env("POOL_MAX_CONTEXTS_PER_BROWSER", 1)
    )
    pool_reserved_transient_contexts: int = field(
        default_factory=lambda: _int_env("POOL_RESERVED_TRANSIENT_CONTEXTS", 1)
    )
    pool_acquire_timeout_ms: int = field(
        default_factory=lambda: _int_env("POOL_ACQUIRE_TIMEOUT_MS", 30000)
    )
    max_sessions: int = field(default_factory=lambda: _int_env("MAX_SESSIONS", 32))
    session_ttl_minutes: int = field(default_factory=lambda: _int_env("SESSION_TTL_MINUTES", 60))
    browser_max_uses: int = field(default_factory=lambda: _int_env("BROWSER_MAX_USES", 200))
    browser_max_age_minutes: int = field(
        default_factory=lambda: _int_env("BROWSER_MAX_AGE_MINUTES", 120)
    )
    max_request_body_bytes: int = field(
        default_factory=lambda: _int_env("MAX_REQUEST_BODY_BYTES", 4_194_304)
    )
    max_response_body_bytes: int = field(
        default_factory=lambda: _int_env("MAX_RESPONSE_BODY_BYTES", 33_554_432)
    )
    max_screenshot_bytes: int = field(
        default_factory=lambda: _int_env("MAX_SCREENSHOT_BYTES", 16_777_216)
    )
    max_solution_bytes: int = field(
        default_factory=lambda: _int_env("MAX_SOLUTION_BYTES", 67_108_864)
    )
    max_timeout_ms: int = field(default_factory=lambda: _int_env("MAX_TIMEOUT_MS", 300_000))
    max_session_ttl_minutes: int = field(
        default_factory=lambda: _int_env("MAX_SESSION_TTL_MINUTES", 1_440)
    )
    session_reaper_interval_seconds: int = field(
        default_factory=lambda: _int_env("SESSION_REAPER_INTERVAL_SECONDS", 30)
    )
    pool_maintenance_interval_seconds: int = field(
        default_factory=lambda: _int_env("POOL_MAINTENANCE_INTERVAL_SECONDS", 15)
    )
    shutdown_timeout_seconds: int = field(
        default_factory=lambda: _int_env("SHUTDOWN_TIMEOUT_SECONDS", 30)
    )
    cleanup_timeout_seconds: int = field(
        default_factory=lambda: _int_env("CLEANUP_TIMEOUT_SECONDS", 10)
    )
    readiness_timeout_ms: int = field(
        default_factory=lambda: _int_env("READINESS_TIMEOUT_MS", 15_000)
    )
    prometheus_enabled: bool = field(default_factory=lambda: _bool_env("PROMETHEUS_ENABLED", False))
    challenge_solver: str = field(default_factory=_challenge_solver_env)
    log_format: str = field(default_factory=lambda: os.getenv("LOG_FORMAT", "text").strip().lower())

    def __post_init__(self) -> None:
        _validate_settings(self)

    @property
    def log_level_value(self) -> int:
        return logging.getLevelNamesMapping().get(self.log_level, logging.INFO)

    @property
    def session_ttl_seconds(self) -> int:
        return self.session_ttl_minutes * 60

    @property
    def browser_max_age_seconds(self) -> int:
        return self.browser_max_age_minutes * 60

    @property
    def pool_acquire_timeout_seconds(self) -> float:
        return self.pool_acquire_timeout_ms / 1000

    @property
    def readiness_timeout_seconds(self) -> float:
        return self.readiness_timeout_ms / 1000

    @property
    def env_proxy(self) -> dict[str, str] | None:
        if not self.proxy_url:
            return None
        proxy = {"server": self.proxy_url}
        if self.proxy_username:
            proxy["username"] = self.proxy_username
        if self.proxy_password:
            proxy["password"] = self.proxy_password
        return proxy

    @property
    def resource_limits(self) -> ResourceLimits:
        return ResourceLimits(
            response_body_bytes=self.max_response_body_bytes,
            screenshot_bytes=self.max_screenshot_bytes,
            solution_bytes=self.max_solution_bytes,
        )


def _validate_settings(settings: Settings) -> None:
    if not 1 <= settings.port <= 65535:
        raise ValueError("PORT must be between 1 and 65535.")
    if settings.pool_min_browsers < 0:
        raise ValueError("POOL_MIN_BROWSERS must be zero or greater.")
    if settings.pool_max_browsers <= 0:
        raise ValueError("POOL_MAX_BROWSERS must be greater than zero.")
    if settings.pool_min_browsers > settings.pool_max_browsers:
        raise ValueError("POOL_MIN_BROWSERS cannot exceed POOL_MAX_BROWSERS.")
    if settings.pool_max_contexts_per_browser <= 0:
        raise ValueError("POOL_MAX_CONTEXTS_PER_BROWSER must be greater than zero.")

    total_capacity = settings.pool_max_browsers * settings.pool_max_contexts_per_browser
    if not 0 <= settings.pool_reserved_transient_contexts <= total_capacity:
        raise ValueError(
            "POOL_RESERVED_TRANSIENT_CONTEXTS must be between zero and total pool capacity."
        )
    if settings.pool_acquire_timeout_ms <= 0:
        raise ValueError("POOL_ACQUIRE_TIMEOUT_MS must be greater than zero.")
    if settings.max_sessions <= 0:
        raise ValueError("MAX_SESSIONS must be greater than zero.")
    if settings.session_ttl_minutes <= 0:
        raise ValueError("SESSION_TTL_MINUTES must be greater than zero.")
    if settings.browser_max_uses <= 0:
        raise ValueError("BROWSER_MAX_USES must be greater than zero.")
    if settings.browser_max_age_minutes <= 0:
        raise ValueError("BROWSER_MAX_AGE_MINUTES must be greater than zero.")
    positive_limits = {
        "MAX_REQUEST_BODY_BYTES": settings.max_request_body_bytes,
        "MAX_RESPONSE_BODY_BYTES": settings.max_response_body_bytes,
        "MAX_SCREENSHOT_BYTES": settings.max_screenshot_bytes,
        "MAX_SOLUTION_BYTES": settings.max_solution_bytes,
        "MAX_TIMEOUT_MS": settings.max_timeout_ms,
        "MAX_SESSION_TTL_MINUTES": settings.max_session_ttl_minutes,
        "SESSION_REAPER_INTERVAL_SECONDS": settings.session_reaper_interval_seconds,
        "POOL_MAINTENANCE_INTERVAL_SECONDS": settings.pool_maintenance_interval_seconds,
        "SHUTDOWN_TIMEOUT_SECONDS": settings.shutdown_timeout_seconds,
        "CLEANUP_TIMEOUT_SECONDS": settings.cleanup_timeout_seconds,
        "READINESS_TIMEOUT_MS": settings.readiness_timeout_ms,
    }
    for name, value in positive_limits.items():
        if value <= 0:
            raise ValueError(f"{name} must be greater than zero.")
    if settings.session_ttl_minutes > settings.max_session_ttl_minutes:
        raise ValueError("SESSION_TTL_MINUTES cannot exceed MAX_SESSION_TTL_MINUTES.")
    if settings.log_format not in {"text", "json"}:
        raise ValueError("LOG_FORMAT must be either text or json.")
    if not settings.camouflare_api_token and not is_loopback_host(settings.host):
        raise ValueError("CAMOUFLARE_API_TOKEN is required when HOST is not a loopback address.")
    if settings.proxy_url:
        try:
            normalize_proxy(settings.env_proxy)
        except CamouflareError as exc:
            raise ValueError(f"PROXY_URL/PROXY_SERVER is invalid: {exc}") from exc


def is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower().rstrip(".")
    if normalized == "localhost":
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def normalize_proxy(proxy: dict[str, Any] | None) -> dict[str, str] | None:
    if not proxy:
        return None
    server = proxy.get("server") or proxy.get("url")
    if not server:
        return None
    if not isinstance(server, str):
        raise _invalid_proxy_error()
    normalized = _normalize_proxy_server(server)

    username = proxy.get("username")
    password = proxy.get("password")
    if username is not None:
        normalized["username"] = str(username)
    if password is not None:
        normalized["password"] = str(password)
    proxy_scheme = urlsplit(normalized["server"]).scheme.lower()
    if proxy_scheme == "socks4" and (normalized.get("username") or normalized.get("password")):
        raise CamouflareError(
            "Request parameter 'proxy' cannot use authentication with SOCKS4.",
            error_code=V1ErrorCode.INVALID_REQUEST,
        )
    if proxy_scheme == "socks5":
        for credential_name in ("username", "password"):
            credential = normalized.get(credential_name)
            if credential is None:
                continue
            try:
                credential_size = len(credential.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise CamouflareError(
                    f"Request parameter 'proxy.{credential_name}' must be valid UTF-8.",
                    error_code=V1ErrorCode.INVALID_REQUEST,
                ) from exc
            if credential_size > 255:
                raise CamouflareError(
                    f"Request parameter 'proxy.{credential_name}' must be at most 255 UTF-8 bytes "
                    "for SOCKS5 authentication.",
                    error_code=V1ErrorCode.INVALID_REQUEST,
                )
    return normalized


def _normalize_proxy_server(server: str) -> dict[str, str]:
    server = server.strip()
    if not server or any(character.isspace() for character in server):
        raise _invalid_proxy_error()
    try:
        parsed = urlsplit(server)
    except ValueError as exc:
        raise _invalid_proxy_error() from exc
    if not parsed.scheme or not parsed.netloc:
        try:
            authority = urlsplit(f"//{server}")
            host = authority.hostname
            _ = authority.port
        except ValueError as exc:
            raise _invalid_proxy_error() from exc
        if (
            host is None
            or authority.path
            or authority.query
            or authority.fragment
            or authority.username is not None
            or authority.password is not None
        ):
            raise _invalid_proxy_error()
        return {"server": server}

    raw_scheme = parsed.scheme.lower()
    if raw_scheme not in {"http", "https", "socks4", "socks5", "socks5h"}:
        raise _invalid_proxy_error()
    scheme = "socks5" if raw_scheme == "socks5h" else raw_scheme
    host = parsed.hostname
    try:
        port = parsed.port
    except ValueError as exc:
        raise _invalid_proxy_error() from exc
    if host is None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise _invalid_proxy_error()

    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    stripped = SplitResult(scheme, netloc, "", "", "")
    normalized = {"server": urlunsplit(stripped)}

    if parsed.username is not None:
        normalized["username"] = unquote(parsed.username)
    if parsed.password is not None:
        normalized["password"] = unquote(parsed.password)
    return normalized


def _invalid_proxy_error() -> CamouflareError:
    return CamouflareError(
        "Request parameter 'proxy' must contain a valid HTTP(S), SOCKS4, or SOCKS5 server.",
        error_code=V1ErrorCode.INVALID_REQUEST,
    )
