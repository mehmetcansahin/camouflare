from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MAX_URL_LENGTH = 16_384
MAX_SESSION_ID_LENGTH = 128
MAX_TARGET_HEADERS = 128
MAX_TARGET_HEADER_BYTES = 65_536
MAX_COOKIES = 300
MAX_COOKIE_BYTES = 262_144


class ResourceLimitError(RuntimeError):
    """Raised when an inbound or outbound resource exceeds a configured limit."""


@dataclass(frozen=True)
class ResourceLimits:
    response_body_bytes: int = 33_554_432
    screenshot_bytes: int = 16_777_216
    solution_bytes: int = 67_108_864


def utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))


def json_size(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    )


def ensure_text_size(value: str, maximum: int, *, label: str) -> None:
    actual = utf8_size(value)
    if actual > maximum:
        raise ResourceLimitError(f"{label} exceeds the configured {maximum}-byte limit.")


def ensure_bytes_size(value: bytes, maximum: int, *, label: str) -> None:
    if len(value) > maximum:
        raise ResourceLimitError(f"{label} exceeds the configured {maximum}-byte limit.")


def ensure_json_size(value: Any, maximum: int, *, label: str) -> None:
    actual = json_size(value)
    if actual > maximum:
        raise ResourceLimitError(f"{label} exceeds the configured {maximum}-byte limit.")
