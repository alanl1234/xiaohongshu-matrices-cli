"""Layered (分层式) analysis of a Xiaohongshu account's published notes.

This is a pure CLI capability: given a Xiaohongshu *user id* (the public
author id, e.g. ``95653634553`` — not the local dashboard account id), it
fetches ALL of that account's notes via the paginated ``user_posted`` API
and produces a hierarchical report:

  L0  账号总览   — inventory + aggregate engagement
  L1  互动分层   — engagement tiers (爆款 / 优质 / 普通 / 潜力)
  L2  主题聚类   — theme clusters (topic tags when available, else title n-grams)
  L3  形式与节奏 — video vs image performance + posting cadence
  L4  头部帖子   — top-N posts by engagement
  L5  战略总结   — prose synthesis (rule-based, optional AI)

The engine only needs the fields the ``user_posted`` list already exposes
(title, liked_count, type, note_id, xsec_token, time). Pass ``--deep`` to
also fetch each note's detail for collects / comments / shares / topics /
body — that enriches L1–L3 but costs one extra request per note.

No dependency on the dashboard database: this keeps the analyzer aligned
with the project rule "批量操作走 CLI" and avoids the Camoufox cookie path.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from .dashboard.utils import parse_count, viral_score

# Engagement tiers by like count (absolute thresholds, Xiaohongshu scale).
_TIERS = [
    ("爆款", 10_000),
    ("优质", 1_000),
    ("普通", 100),
    ("潜力", 0),
]

# Weighted engagement used for ranking when deep metrics are unavailable.
def _weighted(liked: int, collects: int, comments: int, shares: int) -> int:
    return liked * 1 + collects * 3 + comments * 2 + shares * 4


def _timestamp(value: Any) -> str | None:
    """Normalize a timestamp into an ISO string (or None)."""
    if not value:
        return None
    if isinstance(value, str) and "-" in value:
        return value[:19] if "T" in value else value
    try:
        number = float(value)
        if number > 10_000_000_000:  # milliseconds
            number /= 1000
        return datetime.fromtimestamp(number, UTC).isoformat()[:19]
    except (TypeError, ValueError, OSError):
        return None


def fetch_all_user_notes(
    client: Any,
    user_id: str,
    *,
    max_pages: int | None = None,
    delay: float = 0.0,
) -> list[dict[str, Any]]:
    """Paginate ``get_user_notes`` until exhausted (or ``max_pages``)."""
    notes: list[dict[str, Any]] = []
    cursor = ""
    page = 0
    while True:
        page += 1
        data = client.get_user_notes(user_id, cursor=cursor)
        batch = data.get("notes", []) or []
        if not batch:
            break
        notes.extend(batch)
        if not data.get("has_more"):
            break
        cursor = str(data.get("cursor", "") or "")
        if not cursor:
            break
        if max_pages and page >= max_pages:
            break
        if delay:
            time.sleep(delay)
    return notes


def _locate_note_card(detail: Any) -> dict[str, Any]:
    """Find the note_card dict inside a heterogeneous detail response."""
    if not isinstance(detail, dict):
        return {}
    data = detail.get("data") or detail
    if isinstance(data, dict):
        for key in ("note_card", "note", "items"):
            val = data.get(key)
            if isinstance(val, dict):
                return val
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return val[0].get("note_card", val[0])
    for key in ("note_card", "note"):
        if isinstance(detail.get(key), dict):
            return detail[key]
    return {}


def extract_record(note: dict[str, Any], detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """Flatten a list note (+ optional detail) into a uniform analysis record."""
    list_interact = note.get("interact_info") or {}
    card = _locate_note_card(detail) if detail else {}
    card_interact = card.get("interact_info") or {}

    liked = parse_count(card_interact.get("liked_count", list_interact.get("liked_count", 0)))
    collects = parse_count(card_interact.get("collected_count", 0)) if card else 0
    comments = parse_count(card_interact.get("comment_count", 0)) if card else 0
    shares = parse_count(card_interact.get("share_count", 0)) if card else 0

    if card:
        topics = [str(t.get("name", "")) for t in (card.get("tag_list") or []) if t.get("name")]
        body = str(card.get("desc", "") or card.get("title", ""))
        published = _timestamp(card.get("time") or card.get("publish_time") or card.get("last_update_time"))
    else:
        topics = []
        body = ""
        published = _timestamp(
            note.get("time") or note.get("last_update_time") or note.get("create_time")
        )

    media_type = "video" if str(note.get("type", "")).lower() == "video" or note.get("video") else "image"
    title = str(note.get("display_title") or note.get("title") or "")
    note_id = str(note.get("note_id") or note.get("id") or "")
    token = str(note.get("xsec_token") or (detail or {}).get("xsec_token", ""))
    engagement = _weighted(liked, collects, comments, shares)
    vs = viral_score(liked, collects, comments, shares)

    return {
        "note_id": note_id,
        "title": title,
        "media_type": media_type,
        "liked": liked,
        "collects": collects,
        "comments": comments,
        "shares": shares,
        "engagement": engagement,
        "viral_score": vs,
        "topics": topics,
        "body": body,
        "published_at": published,
        "xsec_token": token,
        "url": (
            f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={token}"
            if token
            else f"https://www.xiaohongshu.com/explore/{note_id}"
        ),
    }


# ─── Theme clustering ────────────────────────────────────────────────────────


def _cjk_ngrams(text: str, n: int) -> list[str]:
    grams: list[str] = []
    for run in re.findall(r"[一-鿿]+", text):
        for i in range(len(run) - n + 1):
            grams.append(run[i : i + n])
    return grams


_COMMON_GRAMS = {
    "的", "了", "是", "在", "我", "你", "他", "她", "们", "也", "都", "就",
    "还", "和", "与", "及", "或", "把", "被", "让", "给", "向", "从", "到",
    "这", "那", "个", "些", "么", "啊", "呢", "吧", "哦", "啦", "吗", "怎",
    "为", "以", "之", "其", "而", "于", "将", "要", "会", "能", "可",
    "如", "如果", "因为", "所以", "但是", "一个", "怎么", "什么", "这样",
}


def _title_themes(records: list[dict[str, Any]], top_k: int = 6) -> list[dict[str, Any]]:
    """Cluster posts by title keyword frequency (heuristic, list-only)."""
    counter: Counter[str] = Counter()
    for r in records:
        grams = _cjk_ngrams(r["title"], 2) + _cjk_ngrams(r["title"], 3)
        for g in grams:
            if g in _COMMON_GRAMS or len(g) < 2:
                continue
            counter[g] += 1
    candidates = [g for g, c in counter.items() if c >= 2]
    candidates.sort(key=lambda g: counter[g], reverse=True)
    themes: list[dict[str, Any]] = []
    for kw in candidates[:top_k]:
        members = [r for r in records if kw in r["title"]]
        if not members:
            continue
        avg = sum(m["liked"] for m in members) / len(members)
        themes.append(
            {
                "keyword": kw,
                "count": len(members),
                "avg_liked": round(avg),
                "examples": [m["title"][:24] for m in members[:3]],
            }
        )
    return themes


def _topic_themes(records: list[dict[str, Any]], top_k: int = 6) -> list[dict[str, Any]]:
    """Cluster posts by explicit topic tags (deep mode)."""
    counter: Counter[str] = Counter()
    for r in records:
        for t in r["topics"]:
            counter[t] += 1
    if not counter:
        return []
    themes: list[dict[str, Any]] = []
    for topic, _c in counter.most_common(top_k):
        members = [r for r in records if topic in r["topics"]]
        avg = sum(m["liked"] for m in members) / len(members)
        themes.append(
            {
                "keyword": f"#{topic}",
                "count": len(members),
                "avg_liked": round(avg),
                "examples": [m["title"][:24] for m in members[:3]],
            }
        )
    return themes


# ─── Layer builders ─────────────────────────────────────────────────────────


def _build_overview(records: list[dict[str, Any]], user_info: dict[str, Any], deep: bool) -> dict[str, Any]:
    total = len(records)
    likes = [r["liked"] for r in records] or [0]
    video = sum(1 for r in records if r["media_type"] == "video")
    dates = sorted(d for d in (r["published_at"] for r in records) if d)
    followers = 0
    nickname = ""
    try:
        basic = user_info.get("basic_info", user_info) if isinstance(user_info, dict) else {}
        followers = parse_count(basic.get("fans", basic.get("follows", 0)))
        nickname = basic.get("nickname", "")
    except Exception:
        pass
    avg = sum(likes) / total if total else 0
    median = sorted(likes)[len(likes) // 2] if likes else 0
    # active span in days
    span_days = 0
    if len(dates) >= 2:
        try:
            d0 = datetime.fromisoformat(dates[0])
            d1 = datetime.fromisoformat(dates[-1])
            span_days = max(0, (d1 - d0).days)
        except (ValueError, TypeError):
            span_days = 0
    topic_union = set()
    for r in records:
        topic_union.update(r["topics"])
    return {
        "total_notes": total,
        "nickname": nickname,
        "followers": followers,
        "media_mix": {"video": video, "image": total - video},
        "total_likes": sum(likes),
        "avg_likes": round(avg, 1),
        "median_likes": median,
        "max_likes": max(likes),
        "date_first": dates[0] if dates else None,
        "date_last": dates[-1] if dates else None,
        "span_days": span_days,
        "topics_union_count": len(topic_union),
        "deep": deep,
    }


def _build_tiers(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total = len(records) or 1
    tiers: list[dict[str, Any]] = []
    for name, threshold in _TIERS:
        members = [r for r in records if r["liked"] >= threshold]
        share = round(len(members) / total * 100)
        examples = sorted(members, key=lambda x: x["liked"], reverse=True)[:3]
        tiers.append(
            {
                "tier": name,
                "threshold": threshold,
                "count": len(members),
                "share_pct": share,
                "examples": [
                    {"title": m["title"][:22], "liked": m["liked"], "note_id": m["note_id"]}
                    for m in examples
                ],
            }
        )
    return tiers


def _build_format_layer(records: list[dict[str, Any]]) -> dict[str, Any]:
    groups = {"video": [], "image": []}
    for r in records:
        groups[r["media_type"]].append(r)
    out: dict[str, Any] = {}
    for kind, members in groups.items():
        if members:
            out[kind] = {
                "count": len(members),
                "avg_liked": round(sum(m["liked"] for m in members) / len(members)),
            }
    v = out.get("video", {}).get("avg_liked", 0)
    i = out.get("image", {}).get("avg_liked", 0)
    if v and i:
        winner = "video" if v > i else "image"
    elif v:
        winner = "video"
    elif i:
        winner = "image"
    else:
        winner = "tie"
    out["winner"] = winner
    return out


def _build_cadence(records: list[dict[str, Any]]) -> dict[str, Any]:
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    by_weekday = {w: 0 for w in weekdays}
    by_hour = {h: 0 for h in range(24)}
    count = 0
    for r in records:
        d = r["published_at"]
        if not d:
            continue
        try:
            dt = datetime.fromisoformat(d)
        except (ValueError, TypeError):
            continue
        by_weekday[weekdays[dt.weekday()]] += 1
        by_hour[dt.hour] += 1
        count += 1
    if not count:
        return {"available": False, "by_weekday": {}, "by_hour": {}, "best_window": None}
    top_wd = max(by_weekday, key=by_weekday.get)
    # best 4-hour window
    best_hour = max(range(24), key=lambda h: by_hour[h])
    return {
        "available": True,
        "by_weekday": by_weekday,
        "by_hour": by_hour,
        "best_weekday": top_wd,
        "best_hour": best_hour,
        "best_window": f"{best_hour:02d}:00–{(best_hour + 4) % 24:02d}:00",
    }


def _build_top(records: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    ranked = sorted(records, key=lambda x: x["engagement"], reverse=True)[:top_n]
    return [
        {
            "rank": i + 1,
            "title": r["title"][:30],
            "liked": r["liked"],
            "media_type": r["media_type"],
            "note_id": r["note_id"],
            "url": r["url"],
        }
        for i, r in enumerate(ranked)
    ]


def build_synthesis(report: dict[str, Any]) -> str:
    """Rule-based prose synthesis (used when --ai is off or fails)."""
    ov = report["layer0_overview"]
    fmt = report["layer3_format"]
    lines: list[str] = []
    nickname = ov.get("nickname") or "该账号"
    lines.append(
        f"@{nickname} 共发布 {ov['total_notes']} 篇笔记，"
        f"累计获赞 {ov['total_likes']:,}，平均单篇 {ov['avg_likes']:,} 赞。"
    )
    if ov.get("date_first") and ov.get("date_last"):
        lines.append(
            f"时间跨度 {ov['date_first'][:10]} ~ {ov['date_last'][:10]}（{ov['span_days']} 天），"
            f"覆盖 {ov['topics_union_count']} 个不同话题标签。"
        )
    tiers = report["layer1_tiers"]
    viral = next((t for t in tiers if t["tier"] == "爆款"), None)
    if viral and viral["count"]:
        lines.append(
            f"爆款率 {viral['share_pct']}%（{viral['count']} 篇破万赞），内容杠杆明显。"
        )
    themes = report["layer2_themes"][:3]
    if themes:
        kw = "、".join(t["keyword"] for t in themes)
        lines.append(f"高频主题集中在：{kw}。")
    winner = fmt.get("winner")
    if winner and winner != "tie":
        label = "视频" if winner == "video" else "图文"
        lines.append(f"形式表现：{label}互动更优，建议二创时优先沿用该形式。")
    cad = report["layer4_cadence"]
    if cad.get("available"):
        lines.append(
            f"发布节奏：集中在 {cad['best_weekday']} 的 {cad['best_window']} 时段，可复刻该窗口。"
        )
    top = report["layer4_top"][:1]
    if top:
        lines.append(f"标杆帖子：《{top[0]['title']}》（{top[0]['liked']:,} 赞）。")
    if not ov.get("deep"):
        lines.append("（当前为列表口径，仅含点赞；加 --deep 可补全收藏/评论/正文后重新分层。）")
    return "\n".join(lines)


def build_layered_report(
    records: list[dict[str, Any]],
    user_info: dict[str, Any] | None = None,
    *,
    top_n: int = 10,
    deep: bool = False,
) -> dict[str, Any]:
    """Assemble the full layered report dict from analysis records."""
    if not records:
        return {"empty": True}
    has_topics = any(r["topics"] for r in records)
    report = {
        "user_id": records[0].get("note_id", ""),
        "layer0_overview": _build_overview(records, user_info or {}, deep),
        "layer1_tiers": _build_tiers(records),
        "layer2_themes": _topic_themes(records) if has_topics else _title_themes(records),
        "layer3_format": _build_format_layer(records),
        "layer4_cadence": _build_cadence(records),
        "layer4_top": _build_top(records, top_n),
    }
    report["synthesis_source"] = "rule"
    report["layer5_synthesis"] = build_synthesis(report)
    return report
