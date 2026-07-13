#!/usr/bin/env python3
"""Validate time-bounded security exceptions and render a Trivy ignore file."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

EXCEPTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")


def _load(source: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read {source}: {exc}") from exc

    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("Security allowlist must be an object with version=1.")
    exceptions = payload.get("exceptions")
    if not isinstance(exceptions, list):
        raise ValueError("Security allowlist exceptions must be a list.")
    if not all(isinstance(item, dict) for item in exceptions):
        raise ValueError("Every security exception must be an object.")
    return exceptions


def _validate(exceptions: list[dict[str, Any]], *, today: date) -> list[str]:
    identifiers: list[str] = []
    seen: set[str] = set()

    for index, item in enumerate(exceptions, start=1):
        identifier = item.get("id")
        reason = item.get("reason")
        expires_on = item.get("expires_on")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError(f"Exception #{index} must have a non-empty id.")
        identifier = identifier.strip()
        if EXCEPTION_ID.fullmatch(identifier) is None:
            raise ValueError(
                f"Exception #{index} id must be one token using letters, numbers, . _ : + or -."
            )
        if identifier in seen:
            raise ValueError(f"Duplicate security exception id: {identifier}")
        if not isinstance(reason, str) or len(reason.strip()) < 10:
            raise ValueError(f"Exception {identifier} needs a specific reason (10+ characters).")
        if not isinstance(expires_on, str):
            raise ValueError(f"Exception {identifier} must have expires_on in YYYY-MM-DD form.")
        try:
            expiry = date.fromisoformat(expires_on)
        except ValueError as exc:
            raise ValueError(
                f"Exception {identifier} has invalid expires_on={expires_on!r}."
            ) from exc
        if expiry < today:
            raise ValueError(f"Security exception {identifier} expired on {expiry.isoformat()}.")
        seen.add(identifier)
        identifiers.append(identifier)

    return sorted(identifiers)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    try:
        identifiers = _validate(_load(args.source), today=date.today())
    except ValueError as exc:
        print(f"security allowlist error: {exc}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    rendered = "\n".join(identifiers)
    args.output.write_text(f"{rendered}\n" if rendered else "", encoding="utf-8")
    print(f"Validated {len(identifiers)} active security exception(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
