#!/usr/bin/env python3
"""Report container image growth without making size drift a hard gate."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def _format_bytes(value: int) -> str:
    return f"{value / (1024 * 1024):.1f} MiB"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current", type=int, required=True)
    parser.add_argument("--baseline", type=int, default=0)
    parser.add_argument("--threshold", type=float, default=0.10)
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    result: dict[str, int | float | str | None] = {
        "current_bytes": args.current,
        "baseline_bytes": args.baseline or None,
        "platform": args.platform,
        "threshold": args.threshold,
        "growth": None,
        "status": "no-baseline",
    }
    summary = f"Current {args.platform} image size: {_format_bytes(args.current)}."
    if args.baseline > 0:
        growth = (args.current - args.baseline) / args.baseline
        result["growth"] = growth
        result["status"] = "warning" if growth > args.threshold else "ok"
        summary = (
            f"Current {args.platform} image size: {_format_bytes(args.current)}; "
            f"baseline: {_format_bytes(args.baseline)}; growth: {growth:+.1%}."
        )
        if growth > args.threshold:
            print(
                f"::warning title=Container image size::Image grew by {growth:.1%}, "
                f"above the {args.threshold:.0%} review threshold."
            )
    else:
        print("::notice title=Container image size::No main-tag baseline was available.")

    print(summary)
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if summary_path := os.getenv("GITHUB_STEP_SUMMARY"):
        with Path(summary_path).open("a", encoding="utf-8") as summary_file:
            summary_file.write(f"### Container image size\n\n{summary}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
