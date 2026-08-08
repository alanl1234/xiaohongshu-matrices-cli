"""Isolated Camoufox profiles and in-memory cookie handoff."""

from __future__ import annotations

import csv
import io
import logging
import os
import secrets
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..client import XhsClient
from ..command_normalizers import normalize_xhs_user_payload
from .config import DashboardConfig
from .db import Database
from .utils import is_within, now_iso, safe_name

logger = logging.getLogger(__name__)

HOME_URL = "https://www.xiaohongshu.com/"


class AccountBrowserBusy(RuntimeError):
    """Raised when an account persistent profile is already in use."""


class _CamoufoxNotReady(RuntimeError):
    """Camoufox binary not found — user needs to run `python -m camoufox fetch`."""


# Web-internal QR-code bind session tuning.
_QR_BIND_REFRESH_S = 15  # re-screenshot the QR canvas this often (codes rotate)
_QR_BIND_POLL_S = 2  # cookie/web-session poll cadence
_QR_BIND_TIMEOUT_S = 300  # max time before the session is marked expired


@dataclass
class _QrBindState:
    """Volatile state of an in-progress web QR-code bind session.

    Lives in ``AccountBrowserService._qr_sessions`` and is read by the
    dashboard routes while the (headless) browser loop runs in a worker thread.
    """

    state: str = "starting"  # starting|awaiting_scan|scanned|ready|error|expired|cancelled
    error: str | None = None
    token: str = ""
    qr_path: str | None = None
    qr_generated_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    stop: bool = False
    scanned_detected: bool = False


def _try_launch_camoufox(**kwargs):
    """Launch Camoufox with a friendly error when the binary is missing."""
    try:
        from camoufox.sync_api import Camoufox  # noqa: PLC0415
    except ImportError as exc:
        msg = "camoufox 包未安装，请先执行: uv sync"
        raise _CamoufoxNotReady(msg) from exc
    try:
        return Camoufox(**kwargs)
    except (FileNotFoundError, RuntimeError) as exc:
        msg = (
            "Camoufox 浏览器未下载或无法启动。"
            "请先执行: python -m camoufox fetch\n"
            f"原始错误: {exc}"
        )
        raise _CamoufoxNotReady(msg) from exc


class AccountBrowserService:
    @staticmethod
    def _secure_profile(profile: Path) -> str:
        profile.chmod(stat.S_IRWXU)
        if os.name != "nt":
            return "owner_only"
        identity = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=10,
        )
        row = next(csv.reader(io.StringIO(identity.stdout.strip())))
        sid = row[-1].strip()
        if not sid.startswith("S-1-"):
            raise RuntimeError("无法识别当前 Windows 用户 SID")
        result = subprocess.run(
            [
                "icacls",
                str(profile),
                "/inheritance:r",
                "/grant:r",
                f"*{sid}:(OI)(CI)F",
                "*S-1-5-18:(OI)(CI)F",
                "/T",
                "/C",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode:
            raise RuntimeError(f"无法保护账号浏览器档案：{result.stderr or result.stdout}")
        return "owner_and_system"

    def _ensure_unique_identity(self, account_id: int, user_id: str) -> None:
        duplicate = self.db.fetchone(
            "SELECT id,alias FROM accounts WHERE xhs_user_id=? AND id<>?", (user_id, account_id)
        )
        if duplicate:
            raise RuntimeError(f"该小红书账号已绑定到档案 #{duplicate['id']}（{duplicate['alias']}），拒绝重复绑定")

    def __init__(self, db: Database, config: DashboardConfig):
        self.db = db
        self.config = config
        self._locks_guard = threading.Lock()
        self._profile_locks: dict[int, threading.Lock] = {}
        self._qr_sessions: dict[int, _QrBindState] = {}

    @contextmanager
    def _browser_slot(self, account_id: int):
        with self._locks_guard:
            lock = self._profile_locks.setdefault(account_id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise AccountBrowserBusy("This account browser profile is already in use; wait for the current task")
        try:
            yield
        finally:
            lock.release()

    @staticmethod
    def _profile_in_use(profile: Path) -> bool | None:
        if os.name == "nt":
            escaped = str(profile).replace("'", "''").lower()
            script = (
                "$p='" + escaped + "'; "
                "$m=Get-CimInstance Win32_Process | Where-Object { "
                "($_.Name -match '^(camoufox|firefox)') -and $_.CommandLine -and "
                "$_.CommandLine.ToLower().Contains($p) }; "
                "if ($m) { '1' } else { '0' }"
            )
            try:
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            if result.returncode:
                return None
            return result.stdout.strip().endswith("1")
        # POSIX (macOS / Linux): detect a running camoufox/firefox holding this profile
        needle = str(profile)
        try:
            matched = subprocess.run(
                ["pgrep", "-f", needle],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if matched.returncode not in (0, 1):
            return None
        if matched.returncode == 1:
            return False
        for pid in (p.strip() for p in matched.stdout.split() if p.strip().isdigit()):
            try:
                proc = subprocess.run(
                    ["ps", "-p", pid, "-o", "command="],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            cmd = proc.stdout.lower()
            if ("camoufox" in cmd or "firefox" in cmd) and needle.lower() in cmd:
                return True
        return False

    @staticmethod
    def _remove_parent_lock(profile: Path) -> None:
        lock = profile / "parent.lock"
        if not lock.exists():
            return
        if os.name == "nt":
            subprocess.run(["icacls", str(lock), "/reset"], capture_output=True, timeout=10)
        lock.unlink(missing_ok=True)

    def _prepare_profile(self, profile: Path) -> None:
        if not (profile / "parent.lock").exists():
            return
        in_use = self._profile_in_use(profile)
        if in_use is True:
            raise AccountBrowserBusy("Camoufox is using this account profile; finish or close that browser task first")
        if in_use is None:
            raise AccountBrowserBusy(
                "Unable to determine whether Camoufox is running; the profile lock was preserved for safety"
            )
        self._remove_parent_lock(profile)

    def _cleanup_stale_lock(self, profile: Path) -> None:
        for _ in range(5):
            if not (profile / "parent.lock").exists() or self._profile_in_use(profile) is not False:
                return
            try:
                self._remove_parent_lock(profile)
                return
            except OSError:
                time.sleep(0.2)

    def repair_profile_lock(self, account_id: int) -> None:
        account = self._account(account_id)
        profile = Path(account["profile_dir"]).resolve()
        with self._browser_slot(account_id):
            self._prepare_profile(profile)

    def _account_references(self, account_id: int) -> list[str]:
        references: list[str] = []
        with self.db.connect() as con:
            tables = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                if table == "accounts" or '"' in table:
                    continue
                for foreign_key in con.execute(f'PRAGMA foreign_key_list("{table}")'):
                    if foreign_key[2] != "accounts":
                        continue
                    column = str(foreign_key[3])
                    if '"' in column:
                        continue
                    count = con.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{column}"=?', (account_id,)).fetchone()[
                        0
                    ]
                    if count:
                        references.append(f"{table}.{column}={count}")
        return sorted(references)

    def delete_account(self, account_id: int) -> None:
        """Delete an unused account and its isolated profile without losing history."""
        account = self._account(account_id)
        profile = Path(account["profile_dir"]).resolve()
        trash = profile.parent / f".{profile.name}.deleting-{account_id}-{time.time_ns()}"
        with self._browser_slot(account_id):
            self._prepare_profile(profile)
            references = self._account_references(account_id)
            if references:
                details = ", ".join(references[:5])
                raise ValueError(f"Account has related history ({details}); disable it instead of deleting")
            moved = False
            if profile.exists():
                profile.rename(trash)
                moved = True
            try:
                self.db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            except sqlite3.IntegrityError as exc:
                if moved and trash.exists():
                    trash.rename(profile)
                raise ValueError("Account has related records; disable it instead of deleting") from exc
            except Exception:
                if moved and trash.exists():
                    trash.rename(profile)
                raise
            if moved:
                shutil.rmtree(trash)

    def create_account(self, alias: str, group_name: str = "") -> int:
        if len(self.db.fetchall("SELECT id FROM accounts")) >= self.config.max_accounts:
            raise ValueError(f"最多只能创建 {self.config.max_accounts} 个账号")
        profile = (self.config.profiles_dir / safe_name(alias)).resolve()
        if not is_within(profile, self.config.profiles_dir):
            raise ValueError("账号档案路径无效")
        profile.mkdir(parents=True, exist_ok=True)
        acl_status = self._secure_profile(profile)
        account_id = self.db.create_account(alias.strip(), str(profile), group_name)
        self.db.update("accounts", account_id, profile_acl_status=acl_status)
        return account_id

    def _account(self, account_id: int) -> dict[str, Any]:
        account = self.db.fetchone("SELECT * FROM accounts WHERE id=?", (account_id,))
        if not account:
            raise ValueError("账号不存在")
        profile = Path(account["profile_dir"]).resolve()
        if not is_within(profile, self.config.profiles_dir):
            raise ValueError("账号档案不在受管目录中")
        if account.get("profile_acl_status") not in {"owner_only", "owner_and_system"}:
            acl_status = self._secure_profile(profile)
            self.db.update("accounts", account_id, profile_acl_status=acl_status)
            account["profile_acl_status"] = acl_status
        return account

    @staticmethod
    def _cookie_dict(context: Any) -> dict[str, str]:
        return {
            item["name"]: item["value"] for item in context.cookies() if "xiaohongshu.com" in item.get("domain", "")
        }

    def bind(self, account_id: int, timeout_seconds: int = 300) -> dict[str, Any]:
        account = self._account(account_id)
        profile = Path(account["profile_dir"]).resolve()
        with self._browser_slot(account_id):
            try:
                self._prepare_profile(profile)
                return self._bind(account_id, account, timeout_seconds)
            except AccountBrowserBusy as exc:
                self.db.update("accounts", account_id, last_error=str(exc))
                raise
            finally:
                self._cleanup_stale_lock(profile)

    def _bind(self, account_id: int, account: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
        self.db.update("accounts", account_id, login_status="binding", last_error=None)
        try:
            from camoufox.addons import DefaultAddons

            with _try_launch_camoufox(
                headless=False,
                locale="zh-CN",
                persistent_context=True,
                user_data_dir=account["profile_dir"],
                humanize=True,
                exclude_addons=[DefaultAddons.UBO],
            ) as context:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
                deadline = time.time() + timeout_seconds
                last_error = "等待扫码登录"
                while time.time() < deadline:
                    cookies = self._cookie_dict(context)
                    if cookies.get("a1") and cookies.get("web_session"):
                        try:
                            with XhsClient(cookies) as client:
                                user = normalize_xhs_user_payload(client.get_self_info())
                            if user.get("id") and not user.get("guest"):
                                self._ensure_unique_identity(account_id, str(user["id"]))
                                self.db.update(
                                    "accounts",
                                    account_id,
                                    xhs_user_id=str(user["id"]),
                                    nickname=str(user["nickname"]),
                                    login_status="ready",
                                    last_verified_at=now_iso(),
                                    last_error=None,
                                )
                                return user
                        except Exception as exc:  # session can be incomplete during QR confirmation
                            last_error = str(exc)
                    page.wait_for_timeout(2_000)
                raise TimeoutError(last_error)
        except Exception as exc:
            self.db.update("accounts", account_id, login_status="needs_login", last_error=str(exc))
            raise

    def with_context(self, account_id: int, callback: Callable[[Any], Any], *, headless: bool = True) -> Any:
        account = self._account(account_id)
        profile = Path(account["profile_dir"]).resolve()
        from camoufox.addons import DefaultAddons

        with self._browser_slot(account_id):
            self._prepare_profile(profile)
            try:
                with _try_launch_camoufox(
                    headless=headless,
                    locale="zh-CN",
                    persistent_context=True,
                    user_data_dir=str(profile),
                    exclude_addons=[DefaultAddons.UBO],
                ) as context:
                    return callback(context)
            finally:
                self._cleanup_stale_lock(profile)

    def cookies(self, account_id: int) -> dict[str, str]:
        """Return cookies extracted from a *live* Camoufox browser session.

        ⚠️ DO NOT feed these cookies to ``XhsClient`` API publishing
        (``get_upload_permit`` / ``upload_file`` / ``create_image_note``).
        Xiaohongshu rejects the Camoufox-session cookie with a server-side
        error on the upload-permit call (observed: every call fails). For
        API-based publishing, use the CLI ``xhs post --account <id>`` path
        instead — it bridges the account's cookies **offline** via
        ``account_bridge.get_dashboard_account_cookies()`` (decrypts the
        profile ``cookies.sqlite``) and is verified working.

        This method is reserved for in-browser flows (``bind`` / ``verify``).
        """
        cookies = self.with_context(account_id, self._cookie_dict)
        if not cookies.get("a1"):
            self.db.update("accounts", account_id, login_status="needs_login", last_error="浏览器会话已失效")
            raise RuntimeError("账号需要重新扫码登录")
        return cookies

    def verify(self, account_id: int) -> dict[str, Any]:
        cookies = self.cookies(account_id)
        try:
            with XhsClient(cookies) as client:
                user = normalize_xhs_user_payload(client.get_self_info())
            if not user.get("id") or user.get("guest"):
                raise RuntimeError("当前浏览器会话不是有效登录账号")
            self._ensure_unique_identity(account_id, str(user["id"]))
            self.db.update(
                "accounts",
                account_id,
                xhs_user_id=str(user["id"]),
                nickname=str(user["nickname"]),
                login_status="ready",
                last_verified_at=now_iso(),
                last_error=None,
            )
            return user
        except Exception as exc:
            self.db.update("accounts", account_id, login_status="needs_login", last_error=str(exc))
            raise

    # ─── Web-internal QR-code bind (headless, no desktop window) ─────────────

    def qr_bind_begin(self, account_id: int) -> _QrBindState:
        """Initialise (or reuse) a web QR bind session and return its state."""
        self._account(account_id)  # validate existence early
        existing = self._qr_sessions.get(account_id)
        if existing and existing.state not in ("ready", "error", "expired", "cancelled"):
            return existing  # a live session is already running
        state = _QrBindState(
            state="starting",
            token=secrets.token_hex(16),
            started_at=time.time(),
        )
        self._qr_sessions[account_id] = state
        return state

    def qr_bind_run(self, account_id: int, qr_dir: Path, timeout_seconds: int = _QR_BIND_TIMEOUT_S) -> None:
        """Blocking loop driving a headless Camoufox to complete QR login.

        Must be invoked on a worker thread (e.g. ``executor.submit``). It holds
        the account profile lock and the Camoufox context until login settles or
        the session is cancelled. The QR PNG is written to ``qr_dir`` so the
        dashboard can stream it to the browser.
        """
        account = self._account(account_id)
        profile = Path(account["profile_dir"]).resolve()
        state = self._qr_sessions.get(account_id)
        if state is None:
            return
        qr_path = Path(qr_dir) / f"qr_bind_{account_id}.png"
        state.qr_path = str(qr_path)
        with self._browser_slot(account_id):
            try:
                self._prepare_profile(profile)
                self._qr_bind_loop(account_id, account, qr_path, timeout_seconds, state)
            except AccountBrowserBusy as exc:
                self.db.update("accounts", account_id, login_status="needs_login", last_error=str(exc))
                state.state = "error"
                state.error = str(exc)
            finally:
                self._cleanup_stale_lock(profile)

    def _qr_bind_loop(self, account_id, account, qr_path, timeout_seconds, state):
        from camoufox.addons import DefaultAddons

        self.db.update("accounts", account_id, login_status="binding", last_error=None)
        deadline = time.time() + timeout_seconds
        last_qr_refresh = 0.0
        try:
            with _try_launch_camoufox(
                headless=True,
                locale="zh-CN",
                persistent_context=True,
                user_data_dir=account["profile_dir"],
                humanize=True,
                exclude_addons=[DefaultAddons.UBO],
            ) as context:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(HOME_URL + "login", wait_until="domcontentloaded", timeout=60_000)
                while time.time() < deadline:
                    if state.stop:
                        state.state = "cancelled"
                        state.finished_at = time.time()
                        self.db.update(
                            "accounts", account_id, login_status="needs_login", last_error="用户取消绑定"
                        )
                        return
                    cookies = self._cookie_dict(context)
                    if cookies.get("a1") and cookies.get("web_session"):
                        try:
                            with XhsClient(cookies) as client:
                                user = normalize_xhs_user_payload(client.get_self_info())
                        except Exception as exc:
                            state.error = str(exc)
                        else:
                            if user.get("id") and not user.get("guest"):
                                self._ensure_unique_identity(account_id, str(user["id"]))
                                self.db.update(
                                    "accounts",
                                    account_id,
                                    xhs_user_id=str(user["id"]),
                                    nickname=str(user["nickname"]),
                                    login_status="ready",
                                    last_verified_at=now_iso(),
                                    last_error=None,
                                )
                                state.state = "ready"
                                state.finished_at = time.time()
                                qr_path.unlink(missing_ok=True)
                                return
                    # Refresh QR screenshot (first pass + periodically)
                    if (time.time() - last_qr_refresh > _QR_BIND_REFRESH_S) or not qr_path.exists():
                        try:
                            self._capture_qr(page, qr_path)
                            state.qr_generated_at = time.time()
                            last_qr_refresh = time.time()
                            if state.state in ("starting", "awaiting_scan", "scanned") and not state.stop:
                                state.state = "awaiting_scan"
                            if self._detect_scanned(page) and state.state == "awaiting_scan":
                                state.state = "scanned"
                                state.scanned_detected = True
                        except Exception as exc:
                            if state.state != "ready":
                                state.state = "error"
                                state.error = f"无法抓取登录二维码：{exc}"
                                logger.warning("qr_bind QR capture failed for account %s: %s", account_id, exc)
                    time.sleep(_QR_BIND_POLL_S)
        except Exception as exc:
            self.db.update("accounts", account_id, login_status="needs_login", last_error=str(exc))
            state.state = "error"
            state.error = str(exc)
            return
        # Timed out without a session
        state.state = "expired"
        state.error = "二维码绑定超时（未扫码或未完成确认）"
        self.db.update("accounts", account_id, login_status="needs_login", last_error=state.error)

    @staticmethod
    def _capture_qr(page, qr_path: Path) -> None:
        """Screenshot the login QR code. Prefer the canvas, fall back to an img."""
        try:
            canvas = page.locator("canvas").first
            canvas.wait_for(timeout=8_000)
            canvas.screenshot(path=str(qr_path))
            return
        except Exception:
            pass
        for sel in ("img[src*='qr']", ".qrcode img", ".login-qrcode img", "img[alt*='二维码']"):
            try:
                img = page.locator(sel).first
                img.wait_for(timeout=3_000)
                img.screenshot(path=str(qr_path))
                return
            except Exception:
                continue
        raise RuntimeError("登录页未找到二维码元素（canvas/img）")

    @staticmethod
    def _detect_scanned(page) -> bool:
        """Best-effort detection of the 'scanned, confirm on phone' hint."""
        for text in ("已扫描", "扫码成功", "请在手机上确认", "scanned"):
            try:
                if page.get_by_text(text, exact=False).count() > 0:
                    return True
            except Exception:
                continue
        return False

    def qr_bind_status(self, account_id: int) -> dict[str, Any]:
        state = self._qr_sessions.get(account_id)
        if not state:
            return {"state": "none"}
        active = state.state in ("starting", "awaiting_scan", "scanned")
        return {
            "state": state.state,
            "error": state.error,
            "token": state.token if active else None,
            "qr_age": int(time.time() - state.qr_generated_at) if state.qr_generated_at else None,
            "scanned": state.scanned_detected,
        }

    def qr_bind_image_path(self, account_id: int, token: str) -> str | None:
        state = self._qr_sessions.get(account_id)
        if not state or not state.token or state.token != token:
            return None
        if state.qr_path and Path(state.qr_path).exists():
            return state.qr_path
        return None

    def qr_bind_cancel(self, account_id: int) -> dict[str, Any]:
        state = self._qr_sessions.get(account_id)
        if not state:
            return {"ok": False, "error": "没有进行中的绑定会话"}
        state.stop = True
        return {"ok": True}
