"""Isolated Camoufox profiles and in-memory cookie handoff."""

from __future__ import annotations

import csv
import io
import os
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..client import XhsClient
from ..command_normalizers import normalize_xhs_user_payload
from .config import DashboardConfig
from .db import Database
from .utils import is_within, now_iso, safe_name

HOME_URL = "https://www.xiaohongshu.com/"


class AccountBrowserBusy(RuntimeError):
    """Raised when an account persistent profile is already in use."""


class _CamoufoxNotReady(RuntimeError):
    """Camoufox binary not found — user needs to run `python -m camoufox fetch`."""


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

    def create_account(self, alias: str) -> int:
        if len(self.db.fetchall("SELECT id FROM accounts")) >= self.config.max_accounts:
            raise ValueError(f"最多只能创建 {self.config.max_accounts} 个账号")
        profile = (self.config.profiles_dir / safe_name(alias)).resolve()
        if not is_within(profile, self.config.profiles_dir):
            raise ValueError("账号档案路径无效")
        profile.mkdir(parents=True, exist_ok=True)
        acl_status = self._secure_profile(profile)
        account_id = self.db.create_account(alias.strip(), str(profile))
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
