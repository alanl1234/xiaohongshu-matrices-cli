"""Bridge between the dashboard (multi-account) and the CLI client.

The dashboard stores each account's
cookies in a Camoufox (Firefox) profile as an *encrypted* ``cookies.sqlite`` +
``key4.db`` pair, while the CLI ``XhsClient`` only understands the flat
``cookies.json`` file. There was no way to run CLI commands against a specific
dashboard account without overwriting the global ``cookies.json`` (risk of
cross-posting).

This module closes the gap: given a dashboard account identifier it reads the
profile's cookie database **offline** (``browser_cookie3.firefox`` does the
DPAPI/NSS decryption in-process — no browser launch, no network) and returns a
plain ``{name: value}`` dict the client can consume. It never writes
``cookies.json``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .dashboard.config import DashboardConfig
from .dashboard.db import Database
from .exceptions import NoCookieError

# Firefox-style profile cookie container used by Camoufox.
_COOKIE_FILE = "cookies.sqlite"
_KEY_FILE = "key4.db"


def _load_database() -> Database | None:
    """Best-effort open of the dashboard DB; None if dashboard is unused."""
    try:
        cfg = DashboardConfig.load()
    except Exception:  # dashboard not configured / unreadable
        return None
    # Guard: do not *create* a dashboard just because --account was passed.
    if not cfg.database_path.exists():
        return None
    try:
        return Database(cfg.database_path)
    except Exception:
        return None


def _resolve_account(db: Database, identifier: str | int) -> dict[str, Any]:
    """Find an account row by int id, alias, or xhs_user_id (in that order)."""
    # Numeric id lookup first (most precise).
    if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.isdigit()):
        row = db.fetchone("SELECT * FROM accounts WHERE id=?", (int(identifier),))
        if row:
            return row
    ident = str(identifier)
    row = db.fetchone(
        "SELECT * FROM accounts WHERE alias=? OR xhs_user_id=?",
        (ident, ident),
    )
    if not row:
        raise NoCookieError(
            f"dashboard 账号不存在: {identifier!r} "
            f"（可用 `xhs status` 查看已登录账号，或用 --account <id/别名/xhs_user_id>）"
        )
    return row


def get_dashboard_account_cookies(identifier: str | int) -> dict[str, str]:
    """Return cookies for a dashboard account, decrypting its profile offline.

    Args:
        identifier: dashboard account ``id`` (int/str), ``alias``, or
            ``xhs_user_id``.

    Returns:
        ``{cookie_name: cookie_value}`` dict suitable for ``XhsClient(cookies)``.

    Raises:
        NoCookieError: if the account is unknown, has no cookie files, or the
            cookies lack the ``a1`` session token (i.e. the login has expired).
    """
    db = _load_database()
    if db is None:
        raise NoCookieError(
            "未找到 dashboard 数据（~/.xiaohongshu-cli/dashboard）。"
            "请先在 http://127.0.0.1:8765 扫码登录，或改用 cookies.json 登录。"
        )
    row = _resolve_account(db, identifier)
    account_label = row.get("alias") or row.get("xhs_user_id") or str(row.get("id"))

    profile_dir = row.get("profile_dir")
    if not profile_dir:
        raise NoCookieError(f"账号 {account_label} 没有关联浏览器 profile，请重新扫码登录")
    base = Path(profile_dir).expanduser().resolve()
    cookie_file = base / _COOKIE_FILE
    key_file = base / _KEY_FILE
    if not (cookie_file.exists() and key_file.exists()):
        raise NoCookieError(
            f"账号 {account_label} 的 cookie 文件缺失，请先在 dashboard 重新扫码登录"
        )

    import browser_cookie3

    try:
        jar = browser_cookie3.firefox(
            cookie_file=str(cookie_file),
            key_file=str(key_file),
            domain_name="xiaohongshu.com",
        )
    except Exception as exc:  # decryption failure (e.g. profile locked)
        raise NoCookieError(f"账号 {account_label} 的 cookie 解密失败：{exc}") from exc

    cookies = {c.name: c.value for c in jar}
    if not cookies.get("a1"):
        raise NoCookieError(f"账号 {account_label} 的登录态已失效（缺少 a1 token），请重新扫码登录")
    return cookies


def list_dashboard_accounts() -> list[dict[str, Any]]:
    """Read-only listing of dashboard accounts (for `xhs status`).

    Returns an empty list when the dashboard is not initialised, so callers can
    treat absence gracefully without side effects.
    """
    db = _load_database()
    if db is None:
        return []
    rows = db.fetchall(
        "SELECT id, alias, xhs_user_id, nickname, login_status, profile_dir "
        "FROM accounts ORDER BY id"
    )
    accounts = []
    for r in rows:
        cookie_file = Path(r.get("profile_dir", "")) / _COOKIE_FILE
        accounts.append(
            {
                "id": r.get("id"),
                "alias": r.get("alias"),
                "xhs_user_id": r.get("xhs_user_id"),
                "nickname": r.get("nickname"),
                "login_status": r.get("login_status"),
                "has_cookie_file": cookie_file.exists(),
            }
        )
    return accounts
