"""Helpers that normalize raw client responses for command-layer use."""

from __future__ import annotations

import re
from typing import Any

from .formatter_utils import coerce_int


def normalize_xhs_user_payload(info: dict[str, Any]) -> dict[str, object]:
    """Normalize Xiaohongshu user info for structured command output."""
    basic = info.get("basic_info", info) if isinstance(info, dict) else {}
    if not isinstance(basic, dict):
        basic = {}

    user_id = (
        basic.get("user_id")
        or info.get("user_id")
        or info.get("userid")
        or basic.get("red_id")
        or info.get("red_id")
        or ""
    )

    return {
        "id": user_id,
        "name": basic.get("nickname") or info.get("nickname", "Unknown"),
        "username": basic.get("red_id") or info.get("red_id", ""),
        "nickname": basic.get("nickname") or info.get("nickname", "Unknown"),
        "red_id": basic.get("red_id") or info.get("red_id", ""),
        "ip_location": basic.get("ip_location") or info.get("ip_location", ""),
        "desc": basic.get("desc") or info.get("desc", ""),
        "guest": bool(info.get("guest", False)),
    }


def normalize_unread_summary(data: dict[str, Any]) -> dict[str, int]:
    return {
        "mentions": coerce_int(data.get("mentions", 0)),
        "likes": coerce_int(data.get("likes", 0)),
        "connections": coerce_int(data.get("connections", 0)),
        "unread_count": coerce_int(data.get("unread_count", 0)),
    }


def normalize_paged_notes(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "notes": data.get("notes", []),
        "has_more": bool(data.get("has_more", False)),
        "cursor": data.get("cursor", ""),
    }


def select_topic_payload(topic_data: Any, fallback_name: str) -> list[dict[str, str]]:
    topics = topic_data if isinstance(topic_data, list) else topic_data.get("topic_info_dtos", [])
    if not topics:
        return []
    first = topics[0]
    return [
        {
            "id": first.get("id", ""),
            "name": first.get("name", fallback_name),
            "type": "topic",
        }
    ]


# Characters that never belong in a topic name and break exact search matches.
_TOPIC_SCRUB = re.compile(r"[^\w\u4e00-\u9fff]")


def resolve_topic_payload(client: Any, topic: str, explicit_id: str | None = None):
    """Resolve one topic string into a hash_tag payload.

    Pipeline:
      1. ``search_topics(topic)`` -> first match (exact).
      2. on empty, retry with noise stripped (trailing punctuation/spaces/emoji).
      3. if still empty and ``explicit_id`` is supplied, use it directly — this
         lets the user force-link a topic whose search is flaky.
      4. otherwise the topic cannot be linked and is reported as unresolved.

    A topic that "cannot be linked" becomes plain text in the post body and is
    NOT clickable on Xiaohongshu — callers should warn the user.

    Returns:
        ``(payload, missing)`` — ``payload`` is the ``{"id", "name", "type"}``
        dict, or ``None``; ``missing`` is the topic string when unresolved,
        else ``None``.
    """
    def _search(text: str) -> list[dict[str, str]]:
        try:
            data = client.search_topics(text)
        except Exception:
            return []
        return select_topic_payload(data, text)

    matched = _search(topic)
    if not matched:
        cleaned = _TOPIC_SCRUB.sub("", topic)
        if cleaned and cleaned != topic:
            matched = _search(cleaned)

    if matched:
        return matched[0], None
    if explicit_id:
        return {"id": explicit_id, "name": topic, "type": "topic"}, None
    return None, topic


def resolve_current_user_id(info: dict[str, Any]) -> str:
    return info.get("user_id", "") if isinstance(info, dict) else ""
