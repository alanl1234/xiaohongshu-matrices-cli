"""Reviewed comment and direct-message execution with hard governance gates."""

from __future__ import annotations

import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..client import XhsClient
from .browser import AccountBrowserService
from .config import DashboardConfig
from .governance import contains_opt_out, contains_sensitive_information, evaluate_content, is_warm_lead
from .operations import OperationsStore
from .utils import json_dumps, now_iso

PRO_INBOX_URL = "https://pro.xiaohongshu.com/im/multiCustomerService"
NORMAL_INBOX_URLS = (
    "https://www.xiaohongshu.com/notification",
    "https://www.xiaohongshu.com/message",
)


class EngagementBlocked(RuntimeError):
    pass


class EngagementAmbiguous(RuntimeError):
    pass


class GovernanceService:
    def __init__(self, store: OperationsStore):
        self.store = store

    def inspect_inbound(self, thread_id: int, text: str) -> str:
        thread = self.store.db.fetchone("SELECT * FROM engagement_threads WHERE id=?", (thread_id,))
        if not thread:
            raise ValueError("会话不存在")
        if contains_sensitive_information(text):
            now = now_iso()
            self.store.db.execute(
                "INSERT INTO sensitive_handoff_events(thread_id,account_id,created_at) VALUES(?,?,?)",
                (thread_id, thread["account_id"], now),
            )
            self.store.db.execute(
                "UPDATE engagement_threads SET status='human_handoff',updated_at=? WHERE id=?", (now, thread_id)
            )
            self.store.db.execute(
                "UPDATE engagement_tasks SET status='cancelled',"
                "error='检测到敏感信息，请前往账号人工查看',updated_at=? "
                "WHERE thread_id=? AND status IN ('pending_review','approved','queued')",
                (now, thread_id),
            )
            return "human_handoff"
        if contains_opt_out(text):
            now = now_iso()
            self.store.db.execute(
                """INSERT INTO target_contacts(external_user_id,blocked,block_reason,updated_at)
                VALUES(?,1,'opt_out',?) ON CONFLICT(external_user_id) DO UPDATE SET
                blocked=1,block_reason='opt_out',updated_at=excluded.updated_at""",
                (thread["external_user_id"], now),
            )
            self.store.db.execute(
                "UPDATE engagement_threads SET status='opted_out',updated_at=? WHERE id=?", (now, thread_id)
            )
            self.store.db.execute(
                "UPDATE engagement_tasks SET status='cancelled',error='对方已拒绝继续联系',updated_at=? "
                "WHERE thread_id=? AND status IN ('pending_review','approved','queued')",
                (now, thread_id),
            )
            return "opted_out"
        return "safe"

    def preflight(self, task: dict[str, Any]) -> None:
        rule = self.store.active_rule()
        rules = rule["rules"]
        previous = self.store.recent_content(int(task["account_id"]), str(task["kind"]))
        result = evaluate_content(task["content"], previous, float(rules["similarity_threshold"]))
        self.store.db.execute(
            """INSERT INTO policy_decisions(task_id,rule_id,decision,reasons_json,signals_json,created_at)
            VALUES(?,?,?,?,?,?)""",
            (
                task["id"],
                rule["id"],
                result.decision,
                json_dumps(result.reasons),
                json_dumps({"sensitive": result.sensitive, "opt_out": result.opt_out, "similarity": result.similarity}),
                now_iso(),
            ),
        )
        if result.decision == "block":
            raise EngagementBlocked("；".join(result.reasons))

        account_id, kind = int(task["account_id"]), str(task["kind"])
        account = self.store.db.fetchone("SELECT enabled,login_status FROM accounts WHERE id=?", (account_id,))
        if not account or not account["enabled"]:
            raise EngagementBlocked("账号已停用，账号级停止开关生效")
        if account["login_status"] not in {"ready", "needs_verification"}:
            raise EngagementBlocked("账号当前不可执行互动，请先处理登录或账号异常")
        thread = None
        if task.get("thread_id"):
            thread = self.store.db.fetchone("SELECT * FROM engagement_threads WHERE id=?", (task["thread_id"],))
            if not thread or thread["status"] != "active":
                raise EngagementBlocked("会话已停止自动处理")
        if kind == "dm_outbound":
            if not thread or not thread["warm_lead"] or not is_warm_lead(thread["lead_reason"]):
                raise EngagementBlocked("主动私信仅允许明确意向行为形成的暖线索")
        target_user = str(task.get("target_user_id") or (thread or {}).get("external_user_id") or "")
        if target_user:
            contact = self.store.db.fetchone("SELECT * FROM target_contacts WHERE external_user_id=?", (target_user,))
            if contact and contact["blocked"]:
                raise EngagementBlocked("目标已拒绝触达或被加入停止名单")
            if kind in {"dm_outbound", "comment"} and contact and contact["last_contact_at"]:
                last = datetime.fromisoformat(contact["last_contact_at"])
                if last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                if datetime.now(UTC) - last < timedelta(days=int(rules["target_cooldown_days"])):
                    raise EngagementBlocked("该用户仍处于跨账号触达冷却期")
        self._check_budget(account_id, kind, int(task["thread_id"]) if task.get("thread_id") else None, rules)

    def _check_budget(self, account_id: int, kind: str, thread_id: int | None, rules: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        hour = now.replace(minute=0, second=0, microsecond=0).isoformat()

        def count(sql, params):
            return int((self.store.db.fetchone(sql, params) or {"n": 0})["n"])

        daily = count(
            "SELECT COUNT(*) n FROM operation_actions WHERE account_id=? AND action=? "
            "AND datetime(created_at)>=datetime(?)",
            (account_id, kind, day),
        )
        limits = {
            "comment_reply": int(rules["comment_reply_daily"]),
            "comment": int(rules["external_comment_daily"]),
            "dm_outbound": int(rules["dm_outbound_daily"]),
            "dm_reply": int(rules["dm_inbound_daily"]),
        }
        if daily >= limits[kind]:
            raise EngagementBlocked(f"已达到 {kind} 每账号日限额 {limits[kind]}")
        if kind in {"comment", "comment_reply"}:
            hourly = count(
                "SELECT COUNT(*) n FROM operation_actions WHERE account_id=? AND action IN ('comment','comment_reply') "
                "AND datetime(created_at)>=datetime(?)",
                (account_id, hour),
            )
            if hourly >= int(rules["comment_hourly_combined"]):
                raise EngagementBlocked("评论与回复合计已达到每小时限额")
        if kind == "dm_reply" and thread_id:
            thread_daily = count(
                "SELECT COUNT(*) n FROM operation_actions WHERE account_id=? AND thread_id=? AND action='dm_reply' "
                "AND datetime(created_at)>=datetime(?)",
                (account_id, thread_id, day),
            )
            if thread_daily >= int(rules["dm_thread_daily"]):
                raise EngagementBlocked("该会话已达到每日回复上限")
            self._check_interval(account_id, kind, int(rules["dm_thread_interval_seconds"]), thread_id)
        if kind == "dm_outbound":
            self._check_interval(account_id, kind, int(rules["dm_outbound_interval_seconds"]), None)

    def _check_interval(self, account_id: int, kind: str, seconds: int, thread_id: int | None) -> None:
        sql = "SELECT created_at FROM operation_actions WHERE account_id=? AND action=?"
        params: list[Any] = [account_id, kind]
        if thread_id is not None:
            sql += " AND thread_id=?"
            params.append(thread_id)
        sql += " ORDER BY id DESC LIMIT 1"
        row = self.store.db.fetchone(sql, tuple(params))
        if row:
            last = datetime.fromisoformat(row["created_at"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            remaining = seconds - (datetime.now(UTC) - last).total_seconds()
            if remaining > 0:
                raise EngagementBlocked(f"最小发送间隔未满足，还需等待 {int(remaining) + 1} 秒")

    def record_action(self, task: dict[str, Any]) -> None:
        thread = (
            self.store.db.fetchone("SELECT * FROM engagement_threads WHERE id=?", (task["thread_id"],))
            if task.get("thread_id")
            else None
        )
        external_user_id = str(task.get("target_user_id") or (thread or {}).get("external_user_id") or "")
        now = now_iso()
        self.store.db.execute(
            "INSERT INTO operation_actions(account_id,thread_id,external_user_id,action,content,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (task["account_id"], task.get("thread_id"), external_user_id, task["kind"], task["content"], now),
        )
        if external_user_id:
            self.store.db.execute(
                """INSERT INTO target_contacts(external_user_id,last_account_id,last_contact_at,updated_at)
                VALUES(?,?,?,?) ON CONFLICT(external_user_id) DO UPDATE SET
                last_account_id=excluded.last_account_id,last_contact_at=excluded.last_contact_at,
                updated_at=excluded.updated_at""",
                (external_user_id, task["account_id"], now, now),
            )


class DirectMessageAdapter:
    """Versioned DOM adapter. It never persists message bodies."""

    COMPOSERS = (
        "textarea[placeholder*='消息']",
        "textarea[placeholder*='回复']",
        "div[contenteditable='true']",
        "textarea",
    )
    SEND_BUTTONS = ("button:has-text('发送')", "button[type='submit']", "[class*='send'] button")

    def __init__(self, config: DashboardConfig, browsers: AccountBrowserService):
        self.config, self.browsers = config, browsers

    @staticmethod
    def _first_visible(page: Any, selectors: tuple[str, ...]) -> Any:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    return locator
            except Exception:
                continue
        raise EngagementBlocked("消息页面结构已变化，未找到可用输入框；请人工处理")

    def _open_thread(self, context: Any, thread: dict[str, Any]) -> Any:
        page = context.pages[0] if context.pages else context.new_page()
        ref = str(thread.get("platform_thread_ref") or "")
        candidates = [ref] if ref.startswith("https://") else [PRO_INBOX_URL, *NORMAL_INBOX_URLS]
        for url in candidates:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                body = page.locator("body").inner_text(timeout=5_000)
                if re.search(r"验证码|登录后查看|重新登录|账号异常", body):
                    raise EngagementBlocked("消息页需要人工登录或处理账号验证")
                if ref or thread["external_user_id"] in body or thread.get("display_name", "") in body:
                    return page
            except EngagementBlocked:
                raise
            except Exception:
                continue
        raise EngagementBlocked("无法定位目标私信会话，请先在可见浏览器中确认会话入口")

    def inspect(self, account_id: int, thread: dict[str, Any]) -> str:
        def callback(context: Any) -> str:
            page = self._open_thread(context, thread)
            return page.locator("body").inner_text(timeout=10_000)

        return self.browsers.with_context(account_id, callback, headless=False)

    def send(self, account_id: int, thread: dict[str, Any], content: str) -> str:
        def callback(context: Any) -> str:
            page = self._open_thread(context, thread)
            composer = self._first_visible(page, self.COMPOSERS)
            composer.fill(content)
            button = self._first_visible(page, self.SEND_BUTTONS)
            if not button.is_enabled():
                raise EngagementBlocked("发送按钮不可用")
            button.click()
            page.wait_for_timeout(1_500)
            matches = page.get_by_text(content, exact=True)
            if matches.count() < 1:
                raise EngagementAmbiguous("已点击发送但无法核验消息气泡，禁止自动重复发送")
            return f"dom:{int(time.time())}"

        return self.browsers.with_context(account_id, callback, headless=False)


class EngagementExecutor:
    def __init__(self, store: OperationsStore, config: DashboardConfig, browsers: AccountBrowserService):
        self.store, self.config, self.browsers = store, config, browsers
        self.governance = GovernanceService(store)
        self.dm = DirectMessageAdapter(config, browsers)

    def run(self, task_id: int) -> str:
        task = self.store.db.fetchone("SELECT * FROM engagement_tasks WHERE id=?", (task_id,))
        if not task or task["status"] not in {"approved", "queued", "running"}:
            return str(task["status"] if task else "failed")
        attempt_id = self.store.db.execute(
            "INSERT INTO engagement_attempts(task_id,started_at) VALUES(?,?)", (task_id, now_iso())
        )
        self.store.db.execute(
            "UPDATE engagement_tasks SET status='running',error=NULL,updated_at=? WHERE id=?", (now_iso(), task_id)
        )
        try:
            mode = os.getenv("XHS_ENGAGEMENT_MODE", "shadow").strip().lower()
            if mode == "shadow":
                raise EngagementBlocked("当前为影子模式，只生成和审核草稿，不执行发送")
            if mode == "inbound" and task["kind"] in {"comment", "dm_outbound"}:
                raise EngagementBlocked("当前仅开放自有评论回复和入站私信，主动触达仍处于关闭状态")
            if mode not in {"inbound", "reviewed"}:
                raise EngagementBlocked("无效的 XHS_ENGAGEMENT_MODE，必须为 shadow、inbound 或 reviewed")
            self.governance.preflight(task)
            self.store.db.execute("UPDATE engagement_attempts SET stage='executing' WHERE id=?", (attempt_id,))
            if task["kind"] in {"comment", "comment_reply"}:
                result_ref = self._send_comment(task)
            else:
                thread = self.store.db.fetchone("SELECT * FROM engagement_threads WHERE id=?", (task["thread_id"],))
                if not thread:
                    raise EngagementBlocked("私信任务缺少会话")
                result_ref = self.dm.send(int(task["account_id"]), thread, task["content"])
            self.governance.record_action(task)
            self.store.db.execute(
                "UPDATE engagement_attempts SET status='success',stage='verified',platform_result_ref=?,"
                "finished_at=? WHERE id=?",
                (result_ref, now_iso(), attempt_id),
            )
            self.store.db.execute(
                "UPDATE engagement_tasks SET status='sent',updated_at=? WHERE id=?", (now_iso(), task_id)
            )
            return "sent"
        except EngagementAmbiguous as exc:
            self._finish_failure(task, attempt_id, "verification_pending", "ambiguous", str(exc))
            return "verification_pending"
        except EngagementBlocked as exc:
            self._finish_failure(task, attempt_id, "blocked", "policy", str(exc))
            return "blocked"
        except Exception as exc:
            self._finish_failure(task, attempt_id, "failed", "transient", str(exc))
            return "failed"

    def sync_thread(self, thread_id: int) -> str:
        thread = self.store.db.fetchone("SELECT * FROM engagement_threads WHERE id=?", (thread_id,))
        if not thread:
            return "failed"
        body = self.dm.inspect(int(thread["account_id"]), thread)
        status = self.governance.inspect_inbound(thread_id, body)
        if status == "safe":
            self.store.db.execute(
                "UPDATE engagement_threads SET last_activity_at=?,updated_at=? WHERE id=?",
                (now_iso(), now_iso(), thread_id),
            )
        return status

    def _send_comment(self, task: dict[str, Any]) -> str:
        cookies = self.browsers.cookies(int(task["account_id"]))
        with XhsClient(cookies) as client:
            if task["kind"] == "comment_reply":
                response = client.reply_comment(task["target_note_id"], task["target_comment_id"], task["content"])
            else:
                response = client.post_comment(task["target_note_id"], task["content"])
        if isinstance(response, dict):
            comment_id = response.get("comment_id") or response.get("id") or response.get("data", {}).get("comment_id")
            return str(comment_id or "api-confirmed")
        return "api-confirmed"

    def _finish_failure(self, task: dict[str, Any], attempt_id: int, status: str, category: str, message: str) -> None:
        screenshot = ""
        try:
            screenshot = str(Path(self.config.screenshots_dir) / f"engagement-{task['id']}-{attempt_id}.png")
        except Exception:
            pass
        self.store.db.execute(
            """UPDATE engagement_attempts SET status=?,stage='stopped',error_category=?,message=?,
            screenshot_path=?,finished_at=? WHERE id=?""",
            (status, category, message, screenshot, now_iso(), attempt_id),
        )
        self.store.db.execute(
            "UPDATE engagement_tasks SET status=?,error=?,updated_at=? WHERE id=?",
            (status, message, now_iso(), task["id"]),
        )
