"""Audited, reviewed-task-only publisher using the official creator web UI."""

from __future__ import annotations

import hashlib
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..client import XhsClient
from .browser import AccountBrowserService
from .config import DashboardConfig
from .db import Database
from .utils import json_dumps, json_loads, now_iso

PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


class PublishFlowError(RuntimeError):
    def __init__(self, message: str, category: str, *, retryable: bool = False):
        super().__init__(message)
        self.category = category
        self.retryable = retryable


class VerificationRequired(LookupError):
    pass


class BrowserPublisher:
    def __init__(self, db: Database, config: DashboardConfig, browsers: AccountBrowserService):
        self.db = db
        self.config = config
        self.browsers = browsers
        self._locks: dict[int, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, account_id: int) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(account_id, threading.Lock())

    def _check_cooldown(self, account: dict[str, Any]) -> None:
        value = account.get("last_publish_at")
        if not value:
            return
        elapsed = (datetime.now(UTC) - datetime.fromisoformat(value)).total_seconds()
        remaining = self.config.publish_cooldown_seconds - elapsed
        if remaining > 0:
            raise PublishFlowError(f"账号发布冷却中，还需等待 {int(remaining)} 秒", "cooldown", retryable=True)

    @staticmethod
    def _fingerprint(task: dict[str, Any], images: list[Path]) -> str:
        digest = hashlib.sha256()
        digest.update(str(task["account_id"]).encode())
        digest.update(task["title"].strip().encode("utf-8"))
        digest.update(task["body"].strip().encode("utf-8"))
        for image in images:
            digest.update(image.name.encode("utf-8"))
            digest.update(str(image.stat().st_size).encode())
        return digest.hexdigest()

    def _preflight(self, task: dict[str, Any], account: dict[str, Any]) -> tuple[list[Path], str]:
        if not account.get("enabled") or account.get("login_status") != "ready":
            raise PublishFlowError("目标账号未处于可发布登录状态", "login")
        title = task["title"].strip()
        body = task["body"].strip()
        topics = json_loads(task["topics_json"], [])
        if not 1 <= len(title) <= 20:
            raise PublishFlowError("标题必须为 1–20 个字符", "content")
        if not body:
            raise PublishFlowError("正文不能为空", "content")
        rendered_body = body + "".join(f" #{topic}" for topic in topics)
        if len(rendered_body) > 1000:
            raise PublishFlowError("正文加话题后超过 1000 个字符", "content")
        images = [Path(value).expanduser().resolve() for value in json_loads(task["images_json"], [])]
        if not 1 <= len(images) <= 18:
            raise PublishFlowError("图文笔记必须包含 1–18 张图片", "content")
        for image in images:
            if not image.is_file() or image.suffix.lower() not in IMAGE_SUFFIXES:
                raise PublishFlowError(f"图片不存在或格式不受支持：{image}", "content")
            if image.stat().st_size > 20 * 1024 * 1024:
                raise PublishFlowError(f"单张图片超过 20MB：{image.name}", "content")
        fingerprint = self._fingerprint(task, images)
        existing = self.db.fetchone(
            "SELECT id,status FROM publish_tasks WHERE content_fingerprint=? AND id<>? "
            "AND status IN ('publishing','published','verification_pending') ORDER BY id DESC LIMIT 1",
            (fingerprint, task["id"]),
        )
        if existing:
            raise PublishFlowError(f"检测到相同内容任务 #{existing['id']} 已发布或待核验，拒绝重复提交", "duplicate")
        self.db.update("publish_tasks", int(task["id"]), content_fingerprint=fingerprint)
        return images, rendered_body

    def _first_visible(self, page: Any, selectors: list[str], stage: str, timeout_seconds: int = 45) -> Any:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            self._detect_blocker(page)
            for selector in selectors:
                locator = page.locator(selector).first
                try:
                    if locator.count() and locator.is_visible():
                        return locator
                except Exception:
                    continue
            page.wait_for_timeout(500)
        raise PublishFlowError(f"等待 {stage} 控件超时；创作中心页面结构可能已变化", "selector")

    def _wait_enabled(self, page: Any, locator: Any, timeout_seconds: int = 60) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            self._detect_blocker(page)
            try:
                if locator.is_visible() and not locator.is_disabled():
                    return
            except Exception:
                pass
            page.wait_for_timeout(500)
        raise PublishFlowError("发布按钮持续不可用，请检查页面中的内容提示", "content")

    @staticmethod
    def _detect_blocker(page: Any) -> None:
        url = page.url.lower()
        if "login" in url or page.get_by_text("扫码登录", exact=False).count():
            raise PublishFlowError("账号登录已失效，需要重新扫码", "login")
        patterns = [
            (r"验证码|安全验证|滑块", "captcha", "页面要求人工安全验证"),
            (r"账号异常|账号限制|发布受限|风险提示", "account_restricted", "账号当前受到发布限制"),
            (r"操作频繁|稍后再试|请求频繁", "rate_limited", "平台提示操作频繁，请稍后人工重试"),
        ]
        body_text = ""
        try:
            body_text = page.locator("body").inner_text(timeout=2_000)
        except Exception:
            pass
        for pattern, category, message in patterns:
            if re.search(pattern, body_text):
                raise PublishFlowError(message, category)

    def _wait_for_uploads(self, page: Any, expected: int, timeout_seconds: int = 90) -> None:
        deadline = time.monotonic() + timeout_seconds
        stable = 0
        while time.monotonic() < deadline:
            self._detect_blocker(page)
            text = ""
            try:
                text = page.locator("body").inner_text(timeout=2_000)
            except Exception:
                pass
            if re.search(r"上传失败|图片处理失败|格式不支持", text):
                raise PublishFlowError("创作中心提示图片上传或处理失败", "upload")
            uploading = bool(re.search(r"上传中|处理中|正在上传", text))
            preview_count = 0
            for selector in [".img-preview img", ".image-preview img", "[class*='preview'] img"]:
                try:
                    preview_count = max(preview_count, page.locator(selector).count())
                except Exception:
                    pass
            if not uploading and (preview_count >= expected or preview_count == 0):
                stable += 1
                if stable >= 3:
                    return
            else:
                stable = 0
            page.wait_for_timeout(1_000)
        raise PublishFlowError("等待图片上传完成超时", "upload", retryable=True)

    def _ensure_image_tab(self, page: Any) -> None:
        """Make sure the editor is on the 上传图文 (image) tab, not 上传视频.

        The creator studio opens on 上传视频 by default, and 上传图文 appears
        BOTH as a top-level tab AND inside the 发布笔记 dropdown menu. That
        shared text made naive `:has-text('上传图文')` selectors ambiguous and
        caused earlier browser-publish attempts to silently land in drafts.
        Strategy:
          1. if an image-upload input is already visible -> done;
          2. click the TOP-LEVEL 上传图文 tab (exclude dropdown menu items);
          3. fallback: open 发布笔记 dropdown, then click the 上传图文 menu item.
        All failures are swallowed; the subsequent upload-input lookup will
        surface a clear selector timeout if the tab truly isn't reachable.
        """
        image_input = page.locator("input[type=file][accept*='image']")
        try:
            if image_input.count() and image_input.first.is_visible():
                return
        except Exception:
            pass

        # 2) top-level tab (NOT inside a popup / role=menuitem)
        for sel in [
            "div[role=tab]:has-text('上传图文')",
            "div.tab-item:has-text('上传图文')",
            "a:has-text('上传图文')",
        ]:
            loc = page.locator(sel).first
            try:
                if loc.count() and loc.is_visible() and loc.get_attribute("role") != "menuitem":
                    loc.click(timeout=3000)
                    if image_input.first.is_visible():
                        return
            except Exception:
                continue

        # 3) fallback: 发布笔记 dropdown -> 上传图文 menu item
        try:
            dropdown = page.locator(
                "button:has-text('发布笔记'), [class*='dropdown']:has-text('发布笔记')"
            ).first
            if dropdown.count() and dropdown.is_visible():
                dropdown.click(timeout=3000)
                page.wait_for_timeout(800)
                menu_item = page.locator(
                    "div[role=menuitem]:has-text('上传图文'), li:has-text('上传图文')"
                ).first
                menu_item.click(force=True, timeout=3000)
        except Exception:
            pass

    @staticmethod
    def _read_draft_count(page: Any) -> int | None:
        """Best-effort read of the 草稿箱 badge count (None if undeterminable)."""
        try:
            el = page.locator("text=草稿箱").first
            if el.count():
                m = re.search(r"(\d+)", el.inner_text(timeout=2000))
                return int(m.group(1)) if m else 0
        except Exception:
            return None
        return None

    @staticmethod
    def _extract_notes(data: dict[str, Any]) -> list[dict[str, Any]]:
        notes = data.get("notes") or data.get("note_list") or data.get("data", {}).get("notes") or []
        return [item for item in notes if isinstance(item, dict)]

    @staticmethod
    def _note_id(item: dict[str, Any]) -> str:
        return str(item.get("note_id") or item.get("id") or item.get("noteId") or "")

    def _creator_notes(self, context: Any) -> list[dict[str, Any]]:
        cookies = self.browsers._cookie_dict(context)
        with XhsClient(cookies) as client:
            return self._extract_notes(client.get_creator_note_list(page=0))

    def _verify_new_note(
        self,
        context: Any,
        task: dict[str, Any],
        before_ids: set[str] | None,
        success_visible: bool,
    ) -> tuple[str, str] | None:
        if before_ids is None:
            return None
        for _ in range(3):
            try:
                notes = self._creator_notes(context)
                for item in notes:
                    note_id = self._note_id(item)
                    title = str(item.get("title") or item.get("display_title") or "").strip()
                    if note_id and note_id not in before_ids and title == task["title"].strip():
                        return note_id, f"https://www.xiaohongshu.com/explore/{note_id}"
            except Exception:
                pass
            time.sleep(3)
        if success_visible:
            raise VerificationRequired("页面提示发布成功，但作品列表未出现本次新增笔记；系统不会自动重发")
        return None

    def _browser_publish(
        self, context: Any, task: dict[str, Any], attempt_id: int, images: list[Path], rendered_body: str
    ) -> tuple[str, str]:
        page = context.pages[0] if context.pages else context.new_page()
        self.db.update("publish_attempts", attempt_id, stage="opening_creator")
        page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=90_000)
        self._detect_blocker(page)
        # The studio defaults to 上传视频; switch to 上传图文 first.
        self._ensure_image_tab(page)
        try:
            before_notes = self._creator_notes(context)
            before_ids: set[str] | None = {self._note_id(item) for item in before_notes if self._note_id(item)}
        except Exception:
            before_ids = None
        self.db.update("publish_attempts", attempt_id, before_note_ids_json=json_dumps(sorted(before_ids or set())))

        upload = self._first_visible(page, ["input[type=file][accept*='image']", "input[type=file]"], "图片上传")
        self.db.update("publish_attempts", attempt_id, stage="uploading_images")
        upload.set_input_files([str(path) for path in images])
        self._wait_for_uploads(page, len(images))

        title = self._first_visible(
            page, ["input[placeholder*='标题']", "textarea[placeholder*='标题']", "input.d-text"], "标题"
        )
        body = self._first_visible(
            page, ["div[contenteditable='true']", "textarea[placeholder*='正文']", "textarea"], "正文"
        )
        self.db.update("publish_attempts", attempt_id, stage="filling_content")
        title.fill(task["title"].strip())
        body.fill(rendered_body)
        self._detect_blocker(page)

        # Prefer the dedicated .publishBtn class and exclude the "发布笔记"
        # dropdown and "存草稿" buttons, which also contain 发布 text.
        publish = self._first_visible(
            page,
            [
                "div.publishBtn",
                "button.publishBtn",
                "button:has-text('发布'):not(:has-text('笔记')):not(:has-text('草稿'))",
                "button.primary:has-text('发布')",
            ],
            "发布",
        )
        self._wait_enabled(page, publish)
        self.db.update("publish_attempts", attempt_id, stage="ready_to_submit")
        # Record the draft-box count so we can detect "saved as draft"
        # instead of "published".
        draft_before = self._read_draft_count(page)
        publish.click()
        submitted = now_iso()
        self.db.update("publish_attempts", attempt_id, stage="submitting", submitted_at=submitted)

        deadline = time.monotonic() + 35
        success_visible = False
        while time.monotonic() < deadline:
            self._detect_blocker(page)
            current_url = page.url
            match = re.search(r"(?:explore|note)/([0-9a-f]{16,})", current_url)
            if match:
                return match.group(1), current_url
            try:
                success_visible = page.get_by_text(re.compile("发布成功|提交成功")).count() > 0
            except Exception:
                pass
            if success_visible:
                break
            page.wait_for_timeout(1_000)

        verified = self._verify_new_note(context, task, before_ids, success_visible)
        if verified:
            return verified

        # If the draft-box count increased after clicking 发布, the content was
        # saved as a draft rather than published — fail loudly instead of
        # reporting spurious success.
        draft_after = self._read_draft_count(page)
        if draft_after is not None and draft_before is not None and draft_after > draft_before:
            raise PublishFlowError(
                "点击发布后草稿箱计数增加，内容疑似被存为草稿而非发布成功。"
                "请到 creator.xiaohongshu.com 草稿箱 UI 手动确认/发布。",
                "saved_as_draft",
            )
        if success_visible or before_ids is None:
            raise VerificationRequired("发布结果无法得到新增笔记 ID；请人工核验，系统不会自动重发")
        raise PublishFlowError("未检测到发布成功，也未发现本次新增笔记", "unknown_after_submit")

    def _publish_with_evidence(
        self, context: Any, task: dict[str, Any], attempt_id: int, images: list[Path], rendered_body: str
    ) -> tuple[str, str]:
        try:
            return self._browser_publish(context, task, attempt_id, images, rendered_body)
        except Exception:
            screenshot = self._screenshot_name(int(task["id"]), attempt_id)
            try:
                page = context.pages[0] if context.pages else None
                if page:
                    page.screenshot(path=screenshot, full_page=True)
                    self.db.update("publish_attempts", attempt_id, screenshot_path=screenshot)
            except Exception:
                pass
            raise

    def run(self, task_id: int) -> str:
        task = self.db.fetchone("SELECT * FROM publish_tasks WHERE id=?", (task_id,))
        if not task or task["status"] not in {"approved", "queued", "publishing"}:
            return str(task["status"] if task else "failed")
        account_id = int(task["account_id"])
        with self._lock(account_id):
            account = self.db.fetchone("SELECT * FROM accounts WHERE id=?", (account_id,))
            if not account:
                return "failed"
            attempt_id = self.db.create_attempt(task_id)
            self.db.update(
                "publish_tasks", task_id, status="publishing", attempts=int(task["attempts"]) + 1, error=None
            )
            try:
                images, rendered_body = self._preflight(task, account)
                self._check_cooldown(account)
                note_id, url = self.browsers.with_context(
                    account_id,
                    lambda context: self._publish_with_evidence(context, task, attempt_id, images, rendered_body),
                    headless=False,
                )
                self.db.update(
                    "publish_attempts",
                    attempt_id,
                    status="success",
                    stage="verified",
                    finished_at=now_iso(),
                    final_note_id=note_id,
                    final_url=url,
                )
                self.db.update("publish_tasks", task_id, status="published", final_note_id=note_id, final_url=url)
                self.db.update("accounts", account_id, last_publish_at=now_iso(), last_error=None)
                return "published"
            except VerificationRequired as exc:
                self.db.update(
                    "publish_attempts",
                    attempt_id,
                    status="verification_pending",
                    stage="verification",
                    error_category="ambiguous",
                    message=str(exc),
                    finished_at=now_iso(),
                )
                self.db.update("publish_tasks", task_id, status="verification_pending", error=str(exc))
                return "verification_pending"
            except PublishFlowError as exc:
                attempt = self.db.fetchone("SELECT stage FROM publish_attempts WHERE id=?", (attempt_id,)) or {}
                submitted = attempt.get("stage") in {"submitting", "verification"}
                if submitted:
                    self.db.update(
                        "publish_attempts",
                        attempt_id,
                        status="verification_pending",
                        error_category=exc.category,
                        message=str(exc),
                        finished_at=now_iso(),
                    )
                    self.db.update("publish_tasks", task_id, status="verification_pending", error=str(exc))
                    return "verification_pending"
                category = (
                    exc.category if exc.category == "cooldown" else ("transient" if exc.retryable else exc.category)
                )
                self.db.update(
                    "publish_attempts",
                    attempt_id,
                    status="failed",
                    error_category=category,
                    message=str(exc),
                    finished_at=now_iso(),
                )
                if exc.category == "login":
                    self.db.update("accounts", account_id, login_status="needs_login", last_error=str(exc))
                elif exc.category in {"captcha", "account_restricted", "rate_limited"}:
                    self.db.update("accounts", account_id, login_status="attention_required", last_error=str(exc))
                self.db.update("publish_tasks", task_id, status="failed", error=str(exc))
                return "failed"
            except Exception as exc:
                attempt = self.db.fetchone("SELECT stage FROM publish_attempts WHERE id=?", (attempt_id,)) or {}
                if attempt.get("stage") in {"submitting", "verification"}:
                    self.db.update(
                        "publish_attempts",
                        attempt_id,
                        status="verification_pending",
                        error_category="ambiguous",
                        message=str(exc),
                        finished_at=now_iso(),
                    )
                    self.db.update("publish_tasks", task_id, status="verification_pending", error=str(exc))
                    return "verification_pending"
                self.db.update(
                    "publish_attempts",
                    attempt_id,
                    status="failed",
                    error_category="transient",
                    message=str(exc),
                    finished_at=now_iso(),
                )
                self.db.update("publish_tasks", task_id, status="failed", error=str(exc))
                return "failed"

    def _screenshot_name(self, task_id: int, attempt_id: int) -> str:
        return str(self.config.screenshots_dir / f"task-{task_id}-attempt-{attempt_id}.png")
