"""Tests for account cookie bridging between the dashboard and the CLI client.

These tests never touch the real dashboard or browser: a temp SQLite DB stands
in for the dashboard database and browser_cookie3.firefox is mocked.
"""

from unittest.mock import patch

import pytest

from xhs_cli.dashboard.db import Database
from xhs_cli.exceptions import NoCookieError


class _FakeCookie:
    def __init__(self, name: str, value: str):
        self.name = name
        self.value = value


def _seed_account(tmp_path, monkeypatch, *, with_a1=True, cookie_files=True):
    """Create a temp dashboard DB + one ready account; return (cfg, profile_dir)."""
    data_dir = tmp_path / "dashboard"
    monkeypatch.setenv("XHS_DASHBOARD_DATA", str(data_dir))
    # import after env is set so DashboardConfig.load() picks up the temp dir
    from xhs_cli.dashboard.config import DashboardConfig

    cfg = DashboardConfig.load()
    db = Database(cfg.database_path)
    profile = tmp_path / "profiles" / "testuser"
    profile.mkdir(parents=True, exist_ok=True)
    aid = db.create_account("testuser", str(profile))
    db.update(
        "accounts",
        aid,
        xhs_user_id="000000000000000000000000",
        nickname="TestAccount",
        login_status="ready",
    )
    if cookie_files:
        (profile / "cookies.sqlite").write_bytes(b"x")
        (profile / "key4.db").write_bytes(b"x")
    return cfg, profile


def _fake_jar(with_a1: bool):
    jar = [_FakeCookie("web_session", "xyz")]
    if with_a1:
        jar.append(_FakeCookie("a1", "abc123"))
    return jar


def test_bridge_by_xhs_user_id(tmp_path, monkeypatch):
    _seed_account(tmp_path, monkeypatch)
    with patch("browser_cookie3.firefox", return_value=_fake_jar(True)):
        from xhs_cli.account_bridge import get_dashboard_account_cookies

        cookies = get_dashboard_account_cookies("000000000000000000000000")
    assert cookies["a1"] == "abc123"
    assert cookies["web_session"] == "xyz"


def test_bridge_by_alias(tmp_path, monkeypatch):
    _seed_account(tmp_path, monkeypatch)
    with patch("browser_cookie3.firefox", return_value=_fake_jar(True)):
        from xhs_cli.account_bridge import get_dashboard_account_cookies

        cookies = get_dashboard_account_cookies("testuser")
    assert cookies["a1"] == "abc123"


def test_bridge_by_int_id(tmp_path, monkeypatch):
    _seed_account(tmp_path, monkeypatch)
    with patch("browser_cookie3.firefox", return_value=_fake_jar(True)):
        from xhs_cli.account_bridge import get_dashboard_account_cookies

        cookies = get_dashboard_account_cookies(1)  # first (and only) account id
    assert cookies["a1"] == "abc123"


def test_bridge_unknown_account_raises(tmp_path, monkeypatch):
    _seed_account(tmp_path, monkeypatch)
    with patch("browser_cookie3.firefox", return_value=_fake_jar(True)):
        from xhs_cli.account_bridge import get_dashboard_account_cookies

        with pytest.raises(NoCookieError):
            get_dashboard_account_cookies("does-not-exist")


def test_bridge_expired_cookie_raises(tmp_path, monkeypatch):
    _seed_account(tmp_path, monkeypatch)
    # a1 missing -> treated as expired login
    with patch("browser_cookie3.firefox", return_value=_fake_jar(False)):
        from xhs_cli.account_bridge import get_dashboard_account_cookies

        with pytest.raises(NoCookieError):
            get_dashboard_account_cookies("testuser")


def test_bridge_no_cookie_files_raises(tmp_path, monkeypatch):
    # seed but do NOT create the cookie files
    _seed_account(tmp_path, monkeypatch, cookie_files=False)
    with patch("browser_cookie3.firefox", return_value=_fake_jar(True)):
        from xhs_cli.account_bridge import get_dashboard_account_cookies

        with pytest.raises(NoCookieError):
            get_dashboard_account_cookies("testuser")


def test_list_dashboard_accounts(tmp_path, monkeypatch):
    _seed_account(tmp_path, monkeypatch)
    with patch("browser_cookie3.firefox", return_value=_fake_jar(True)):
        from xhs_cli.account_bridge import list_dashboard_accounts

        accounts = list_dashboard_accounts()
    assert len(accounts) == 1
    acc = accounts[0]
    assert acc["xhs_user_id"] == "000000000000000000000000"
    assert acc["nickname"] == "TestAccount"
    assert acc["login_status"] == "ready"
    assert acc["has_cookie_file"] is True


def test_list_returns_empty_when_no_dashboard(tmp_path, monkeypatch):
    # Point at an empty dir that has NO database file yet.
    monkeypatch.setenv("XHS_DASHBOARD_DATA", str(tmp_path / "empty"))
    from xhs_cli.account_bridge import list_dashboard_accounts

    assert list_dashboard_accounts() == []
