"""Search, throttled collection, resumable filtering, and export orchestration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from ..client import XhsClient
from ..exceptions import IpBlockedError, NeedVerifyError, SessionExpiredError
from .browser import AccountBrowserService
from .db import Database
from .exporter import MarkdownExporter
from .persistence import AccountTemporarilyPaused, DailyLimitReached, P0Store
from .rate_limit import AccountRateLimiter
from .utils import json_dumps, json_loads, now_iso, parse_count, viral_score

T = TypeVar("T")


def _timestamp(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, str) and "-" in value:
        return value
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def normalize_note(note_id: str, token: str, raw: dict[str, Any], weights: dict[str, float]) -> dict[str, Any]:
    if raw.get("items"):
        note = raw["items"][0].get("note_card", raw["items"][0])
    elif isinstance(raw.get("note"), dict):
        note = raw["note"]
    else:
        note = raw
    user = note.get("user") or note.get("user_info") or {}
    interact = note.get("interact_info") or note.get("interactInfo") or {}
    likes = parse_count(interact.get("liked_count", interact.get("likedCount", note.get("liked_count", 0))))
    collects = parse_count(interact.get("collected_count", interact.get("collectedCount", 0)))
    comments = parse_count(interact.get("comment_count", interact.get("commentCount", 0)))
    shares = parse_count(interact.get("share_count", interact.get("shareCount", 0)))
    image_list = note.get("image_list") or note.get("imageList") or []
    images = []
    for image in image_list:
        if isinstance(image, str):
            images.append(image)
        elif isinstance(image, dict):
            url = image.get("url_default") or image.get("urlDefault") or image.get("url_pre") or image.get("url")
            if url:
                images.append(url)
    tag_list = note.get("tag_list") or note.get("tagList") or []
    topics = [str(tag.get("name", "")) for tag in tag_list if isinstance(tag, dict) and tag.get("name")]
    media_type = "video" if str(note.get("type", "")).lower() == "video" or note.get("video") else "image"
    return {
        "note_id": note_id,
        "author_id": str(user.get("user_id", user.get("userId", ""))),
        "author_name": str(user.get("nickname", user.get("nick_name", ""))),
        "title": str(note.get("title", note.get("display_title", note.get("displayTitle", "")))),
        "body": str(note.get("desc", note.get("description", ""))),
        "published_at": _timestamp(note.get("time") or note.get("publish_time") or note.get("last_update_time")),
        "media_type": media_type,
        "original_url": f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={token}"
        if token
        else f"https://www.xiaohongshu.com/explore/{note_id}",
        "likes": likes,
        "collects": collects,
        "comments": comments,
        "shares": shares,
        "viral_score": viral_score(likes, collects, comments, shares, weights),
        "topics_json": json_dumps(topics),
        "images_json": json_dumps(images),
        "comments_json": "[]",
        "xsec_token": token,
        "xsec_source": "pc_search",
        "raw_json": json_dumps(raw),
    }


class CollectorService:
    def __init__(
        self,
        db: Database,
        browsers: AccountBrowserService,
        exporter: MarkdownExporter,
        limiter: AccountRateLimiter,
        store: P0Store,
    ):
        self.db = db
        self.browsers = browsers
        self.exporter = exporter
        self.limiter = limiter
        self.store = store

    def account_id(self, job: dict[str, Any]) -> int:
        if job.get("account_id"):
            return int(job["account_id"])
        account = self.db.fetchone(
            "SELECT id FROM accounts WHERE enabled=1 AND login_status='ready' ORDER BY id LIMIT 1"
        )
        if not account:
            raise RuntimeError("没有可用的已登录账号")
        return int(account["id"])

    def _call(self, account_id: int, operation: Callable[[], T]) -> T:
        self.limiter.acquire(account_id)
        return operation()

    def _candidates(self, client: XhsClient, job: dict[str, Any], account_id: int) -> dict[str, dict[str, str]]:
        candidates: dict[str, dict[str, str]] = {}
        terms = json_loads(job["keywords_json"], []) + json_loads(job["topics_json"], [])
        note_type = {"all": 0, "video": 1, "image": 2}.get(job["media_type"], 0)
        for term in terms:
            for page in range(1, int(job["max_pages"]) + 1):
                data = self._call(
                    account_id,
                    lambda term=term, page=page: client.search_notes(
                        str(term), page=page, sort="popularity_descending", note_type=note_type
                    ),
                )
                for item in data.get("items", []):
                    card = item.get("note_card", {})
                    note_id = str(item.get("id") or card.get("note_id") or "")
                    token = str(item.get("xsec_token") or card.get("xsec_token") or "")
                    if note_id:
                        candidates[note_id] = {"token": token, "source": "pc_search"}
                if not data.get("has_more"):
                    break
        for author_id in json_loads(job["author_ids_json"], []):
            cursor = ""
            for _ in range(int(job["max_pages"])):
                data = self._call(
                    account_id,
                    lambda author_id=author_id, cursor=cursor: client.get_user_notes(str(author_id), cursor=cursor),
                )
                for item in data.get("notes", []):
                    note_id = str(item.get("note_id") or item.get("id") or "")
                    if note_id:
                        candidates[note_id] = {"token": str(item.get("xsec_token", "")), "source": "pc_profile"}
                cursor = str(data.get("cursor", ""))
                if not data.get("has_more") or not cursor:
                    break
        return candidates

    @staticmethod
    def _matches(job: dict[str, Any], note: dict[str, Any]) -> bool:
        author_ids = json_loads(job["author_ids_json"], [])
        if author_ids and note["author_id"] and note["author_id"] not in author_ids:
            return False
        if job["media_type"] != "all" and note["media_type"] != job["media_type"]:
            return False
        published = (note.get("published_at") or "")[:10]
        if job.get("start_date") and published and published < job["start_date"]:
            return False
        if job.get("end_date") and published and published > job["end_date"]:
            return False
        return (
            note["viral_score"] >= job["min_score"]
            and note["likes"] >= job["min_likes"]
            and note["collects"] >= job["min_collects"]
            and note["comments"] >= job["min_comments"]
        )

    def _comments(self, client: XhsClient, note: dict[str, Any], limit: int, account_id: int) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        cursor = ""
        while len(collected) < limit:
            data = self._call(
                account_id,
                lambda cursor=cursor: client.get_comments(
                    note["note_id"],
                    cursor=cursor,
                    xsec_token=note["xsec_token"],
                    xsec_source=note["xsec_source"],
                ),
            )
            for item in data.get("comments", []):
                user = item.get("user_info") or {}
                collected.append(
                    {
                        "comment_id": item.get("id", ""),
                        "nickname": user.get("nickname", ""),
                        "user_id": user.get("user_id", ""),
                        "content": item.get("content", ""),
                        "like_count": parse_count(item.get("like_count", 0)),
                        "created_at": _timestamp(item.get("create_time")),
                    }
                )
                if len(collected) >= limit:
                    break
            cursor = str(data.get("cursor", ""))
            if not data.get("has_more") or not cursor:
                break
        return collected

    def _save_candidates(self, job_id: int, candidates: dict[str, dict[str, str]]) -> None:
        now = now_iso()
        with self.db.connect() as con:
            con.executemany(
                """INSERT INTO search_candidates(job_id,note_id,token,source,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(job_id,note_id) DO UPDATE SET token=excluded.token,
                source=excluded.source,updated_at=excluded.updated_at""",
                [(job_id, note_id, value["token"], value["source"], now) for note_id, value in candidates.items()],
            )
            con.commit()

    def _set_candidate(self, job_id: int, note_id: str, status: str, error: str | None = None) -> None:
        with self.db.connect() as con:
            con.execute(
                "UPDATE search_candidates SET status=?,last_error=?,updated_at=? WHERE job_id=? AND note_id=?",
                (status, error, now_iso(), job_id, note_id),
            )
            con.commit()

    def run(self, job_id: int) -> str:
        job = self.db.fetchone("SELECT * FROM search_jobs WHERE id=?", (job_id,))
        if not job:
            return "failed"
        account_id = self.account_id(job)
        self.db.update("search_jobs", job_id, status="running", started_at=now_iso(), error=None)
        try:
            cookies = self.browsers.cookies(account_id)
            weights = json_loads(job["weights_json"], {"likes": 1, "collects": 2, "comments": 3, "shares": 1})
            with XhsClient(cookies) as client:
                existing = self.db.fetchone("SELECT COUNT(*) count FROM search_candidates WHERE job_id=?", (job_id,))
                if not existing or not existing["count"]:
                    self._save_candidates(job_id, self._candidates(client, job, account_id))
                with self.db.connect() as con:
                    con.execute(
                        "UPDATE search_candidates SET status='pending',updated_at=? "
                        "WHERE job_id=? AND status IN ('processing','failed')",
                        (now_iso(), job_id),
                    )
                    con.commit()
                total = self.db.fetchone("SELECT COUNT(*) count FROM search_candidates WHERE job_id=?", (job_id,))[
                    "count"
                ]
                done = self.db.fetchone(
                    "SELECT COUNT(*) count FROM search_candidates WHERE job_id=? AND status IN ('accepted','rejected')",
                    (job_id,),
                )["count"]
                accepted = self.db.fetchone(
                    "SELECT COUNT(*) count FROM search_candidates WHERE job_id=? AND status='accepted'", (job_id,)
                )["count"]
                self.db.update(
                    "search_jobs", job_id, progress_total=total, progress_current=done, result_count=accepted
                )
                pending = self.db.fetchall(
                    "SELECT * FROM search_candidates WHERE job_id=? AND status='pending' ORDER BY note_id", (job_id,)
                )
                for row in pending:
                    state = self.db.fetchone("SELECT status FROM search_jobs WHERE id=?", (job_id,))
                    if not state or state["status"] in {"paused", "cancelled"}:
                        return str(state["status"] if state else "cancelled")
                    note_id = str(row["note_id"])
                    self._set_candidate(job_id, note_id, "processing")
                    try:
                        raw = self._call(
                            account_id,
                            lambda row=row: client.get_note_detail(
                                row["note_id"], xsec_token=row["token"], xsec_source=row["source"]
                            ),
                        )
                        note = normalize_note(note_id, str(row["token"]), raw, weights)
                        note["xsec_source"] = str(row["source"])
                        if self._matches(job, note):
                            if job["include_comments"] and note["xsec_token"]:
                                note["comments_json"] = json_dumps(
                                    self._comments(client, note, int(job["comment_limit"]), account_id)
                                )
                            note_db_id = self.db.upsert_note(note)
                            self.db.link_job_note(job_id, note_db_id)
                            stored = self.db.fetchone("SELECT * FROM notes WHERE id=?", (note_db_id,))
                            self.exporter.export(job, stored or note)
                            accepted += 1
                            candidate_status = "accepted"
                        else:
                            candidate_status = "rejected"
                        self._set_candidate(job_id, note_id, candidate_status)
                        done += 1
                        self.db.update("search_jobs", job_id, progress_current=done, result_count=accepted)
                    except (NeedVerifyError, IpBlockedError, SessionExpiredError):
                        self._set_candidate(job_id, note_id, "pending")
                        raise
                    except Exception as exc:
                        self._set_candidate(job_id, note_id, "failed", str(exc))
                        done += 1
                        self.db.update("search_jobs", job_id, progress_current=done, result_count=accepted)
                        continue
            failed = self.db.fetchone(
                "SELECT COUNT(*) count FROM search_candidates WHERE job_id=? AND status='failed'", (job_id,)
            )["count"]
            warning = f"{failed} candidate(s) failed; successful results were preserved" if failed else None
            self.db.update(
                "search_jobs",
                job_id,
                status="complete",
                progress_current=done,
                result_count=accepted,
                error=warning,
                finished_at=now_iso(),
            )
            return "complete"
        except (NeedVerifyError, IpBlockedError) as exc:
            self.limiter.pause(account_id, str(exc))
            self.db.update("accounts", account_id, login_status="attention_required", last_error=str(exc))
            self.db.update("search_jobs", job_id, status="paused", error=str(exc), finished_at=now_iso())
            return "paused"
        except SessionExpiredError as exc:
            self.db.update("accounts", account_id, login_status="needs_login", last_error=str(exc))
            self.db.update("search_jobs", job_id, status="paused", error=str(exc), finished_at=now_iso())
            return "paused"
        except (DailyLimitReached, AccountTemporarilyPaused) as exc:
            self.db.update("search_jobs", job_id, status="paused", error=str(exc), finished_at=now_iso())
            return "paused"
        except Exception as exc:
            self.db.update("search_jobs", job_id, status="failed", error=str(exc), finished_at=now_iso())
            return "failed"
