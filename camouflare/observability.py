from __future__ import annotations

import copy
import json
import logging
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from types import TracebackType
from typing import TextIO, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

MAX_REQUEST_ID_LENGTH = 128
REDACTED = "<redacted>"
REDACTED_URL = "<redacted-url>"

_REQUEST_ID: ContextVar[str | None] = ContextVar("camouflare_request_id", default=None)
_URL_PATTERN = re.compile(r"(?i)\b(?:https?|socks5h?|ftp)://[^\s<>\"']+")
_REQUEST_TARGET_PATTERN = re.compile(
    r"(?i)(\b(?:GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS|TRACE|CONNECT)\s+)(\S+)"
)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|"
    r"api[-_]?key|api[-_]?token|token|password|passwd|secret|"
    r"proxy[-_]?username|proxy[-_]?password|post[-_]?data|request[-_]?body)"
    r"(\s*[:=]\s*)([^,;\n]+)"
)
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "proxyauthorization",
        "cookie",
        "setcookie",
        "apikey",
        "apitoken",
        "token",
        "accesstoken",
        "refreshtoken",
        "password",
        "passwd",
        "secret",
        "clientsecret",
        "username",
        "proxyusername",
        "proxypassword",
        "postdata",
        "body",
        "requestbody",
        "responsebody",
        "payload",
    }
)
_URL_KEYS = frozenset({"url", "uri", "server", "endpoint", "proxyurl", "proxyserver"})
_STANDARD_LOG_RECORD_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
        "request_id",
    }
)


def is_valid_request_id(value: str | None) -> bool:
    """Return whether a header value is non-empty printable ASCII of bounded length."""

    return bool(
        value
        and len(value) <= MAX_REQUEST_ID_LENGTH
        and all(0x20 <= ord(character) <= 0x7E for character in value)
    )


def resolve_request_id(candidate: str | None) -> str:
    if is_valid_request_id(candidate):
        assert candidate is not None
        return candidate
    return str(uuid4())


def get_request_id() -> str | None:
    return _REQUEST_ID.get()


def bind_request_id(request_id: str | None) -> Token[str | None]:
    """Bind a validated request id, generating one if the candidate is unsafe."""

    return _REQUEST_ID.set(resolve_request_id(request_id))


def reset_request_id(token: Token[str | None]) -> None:
    _REQUEST_ID.reset(token)


@contextmanager
def request_id_context(candidate: str | None) -> Iterator[str]:
    request_id = resolve_request_id(candidate)
    token = _REQUEST_ID.set(request_id)
    try:
        yield request_id
    finally:
        _REQUEST_ID.reset(token)


def redact_url(value: str) -> str:
    """Remove userinfo, query, and fragment components from a URL."""

    try:
        parsed = urlsplit(value)
    except ValueError:
        return REDACTED_URL
    if not parsed.scheme or not parsed.netloc:
        if value.startswith(("/", "*")):
            return urlunsplit(("", "", parsed.path, "", ""))
        # A query-like value without a safely identifiable path is not useful
        # enough to justify retaining potentially sensitive text.
        return REDACTED_URL if "?" in value or "@" in value else value
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def redact_text(value: str) -> str:
    """Best-effort sanitization for already-rendered log messages."""

    sanitized = _URL_PATTERN.sub(lambda match: redact_url(match.group(0)), value)
    sanitized = _REQUEST_TARGET_PATTERN.sub(
        lambda match: f"{match.group(1)}{redact_url(match.group(2))}",
        sanitized,
    )
    return _SECRET_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}",
        sanitized,
    )


def redact_mapping(values: Mapping[str, object]) -> dict[str, object]:
    return {str(key): _redact_value(value, key=str(key)) for key, value in values.items()}


def _normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _is_sensitive_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return normalized in _SENSITIVE_KEYS or normalized.endswith(("token", "password", "secret"))


def _is_url_key(key: str) -> bool:
    return _normalized_key(key) in _URL_KEYS


def _redact_value(value: object, *, key: str = "") -> object:
    if _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, Mapping):
        return redact_mapping({str(child_key): child for child_key, child in value.items()})
    if isinstance(value, str):
        return redact_url(value) if _is_url_key(key) else redact_text(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_redact_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(repr(value))


class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        return True


class TextLogFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )

    def format(self, record: logging.LogRecord) -> str:
        safe_record = copy.copy(record)
        safe_record.msg = redact_text(record.getMessage())
        safe_record.args = ()
        safe_record.request_id = getattr(record, "request_id", get_request_id() or "-")
        return super().format(safe_record)

    def formatException(self, ei: object) -> str:
        exc_info = cast(
            "tuple[type[BaseException], BaseException, TracebackType | None]",
            ei,
        )
        return redact_text(super().formatException(exc_info))

    def formatStack(self, stack_info: str) -> str:
        return redact_text(super().formatStack(stack_info))


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        request_id = getattr(record, "request_id", get_request_id())
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "request_id": request_id if is_valid_request_id(request_id) else None,
            "message": redact_text(record.getMessage()),
        }
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_LOG_RECORD_FIELDS
        }
        if extras:
            payload["fields"] = redact_mapping(extras)
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = redact_text(self.formatStack(record.stack_info))
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def configure_logging(
    *,
    level: int | str = logging.INFO,
    log_format: str = "text",
    stream: TextIO | None = None,
    replace_handlers: bool = True,
) -> logging.Handler:
    """Configure the root logger with request context and safe text or JSON output."""

    normalized_format = log_format.strip().lower()
    if normalized_format not in {"text", "json"}:
        raise ValueError("log_format must be either text or json.")

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setLevel(level)
    handler.addFilter(RequestContextFilter())
    handler.setFormatter(JsonLogFormatter() if normalized_format == "json" else TextLogFormatter())

    root = logging.getLogger()
    root.setLevel(level)
    if replace_handlers:
        for existing_handler in root.handlers[:]:
            root.removeHandler(existing_handler)
        # When an external `uvicorn camouflare.asgi:app` process imports the ASGI
        # module, Uvicorn may already have non-propagating handlers installed.
        # Route those records through the same redacting formatter as application
        # logs so request-target query strings cannot bypass LOG_FORMAT policy.
        for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            configured_logger = logging.getLogger(logger_name)
            configured_logger.handlers.clear()
            configured_logger.propagate = True
    root.addHandler(handler)
    return handler
