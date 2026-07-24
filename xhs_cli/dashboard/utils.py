"""Shared normalization helpers for the dashboard."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

COUNT_SUFFIXES = {"亿": 100_000_000, "万": 10_000, "w": 10_000, "W": 10_000, "千": 1_000, "k": 1_000, "K": 1_000}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_count(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip().replace(",", "").replace("+", "")
    multiplier = 1
    if text and text[-1] in COUNT_SUFFIXES:
        multiplier = COUNT_SUFFIXES[text[-1]]
        text = text[:-1]
    try:
        return max(0, int(float(text) * multiplier))
    except ValueError:
        digits = re.sub(r"[^0-9]", "", text)
        return int(digits) if digits else 0


def viral_score(likes: Any, collects: Any, comments: Any, shares: Any, weights: dict[str, float] | None = None) -> int:
    weights = weights or {"likes": 1, "collects": 2, "comments": 3, "shares": 1}
    return int(
        parse_count(likes) * weights["likes"]
        + parse_count(collects) * weights["collects"]
        + parse_count(comments) * weights["comments"]
        + parse_count(shares) * weights["shares"]
    )


def split_terms(value: str | Iterable[str]) -> list[str]:
    values = re.split(r"[,，\n]+", value) if isinstance(value, str) else value
    return list(dict.fromkeys(str(item).strip().lstrip("#") for item in values if str(item).strip()))


def safe_name(value: str, fallback: str = "item") -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value).strip(" ._")
    return cleaned[:100] or fallback


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def content_checksum(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
