from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xhs_cli.dashboard.app import create_app
from xhs_cli.dashboard.browser import AccountBrowserBusy, AccountBrowserService
from xhs_cli.dashboard.config import DashboardConfig
from xhs_cli.dashboard.db import Database
from xhs_cli.dashboard.persistence import DailyLimitReached, P0Store
from xhs_cli.dashboard.publisher import BrowserPublisher, PublishFlowError
from xhs_cli.dashboard.utils import json_dumps


def make_account(db: Database, tmp_path: Path, alias: str) -> int:
    account_id = db.create_account(alias, str(tmp_path / alias))
    db.update("accounts", account_id, login_status="ready")
    return account_id


def test_p0_migration_creates_backup_and_columns(tmp_path):
    db = Database(tmp_path / "dashboard.sqlite3")
    db.create_account("existing", str(tmp_path / "profile"))
    P0Store(db)
    version = db.fetchone("SELECT value FROM schema_meta WHERE key='schema_version'")
    columns = {row["name"] for row in db.fetchall("PRAGMA table_info(publish_attempts)")}
    assert version["value"] == "2"
    assert {"submitted_at", "error_category", "before_note_ids_json"} <= columns
    assert list((tmp_path / "backups").glob("dashboard-before-v2-*.sqlite3"))


def test_queue_is_idempotent_and_serial_per_account(tmp_path):
    db = Database(tmp_path / "dashboard.sqlite3")
    store = P0Store(db)
    account_id = make_account(db, tmp_path, "one")
    first = store.enqueue("search", 1, account_id)
    assert store.enqueue("search", 1, account_id) == first
    store.enqueue("search", 2, account_id)
    claimed = store.claim()
    assert claimed and claimed.resource_id == 1
    assert store.claim() is None
    store.finish(claimed, "done")
    second = store.claim()
    assert second and second.resource_id == 2


def test_queue_allows_cross_account_low_concurrency(tmp_path):
    db = Database(tmp_path / "dashboard.sqlite3")
    store = P0Store(db)
    first_account = make_account(db, tmp_path, "one")
    second_account = make_account(db, tmp_path, "two")
    store.enqueue("search", 1, first_account)
    store.enqueue("search", 2, second_account)
    assert store.claim().account_id == first_account
    assert store.claim().account_id == second_account


def test_expired_publish_is_never_automatically_retried(tmp_path):
    db = Database(tmp_path / "dashboard.sqlite3")
    store = P0Store(db)
    account_id = make_account(db, tmp_path, "one")
    task_id = db.create_publish_task(account_id, "标题", "正文", "[]", '["a.jpg"]')
    db.update("publish_tasks", task_id, status="publishing")
    store.enqueue("publish", task_id, account_id)
    claimed = store.claim(lease_seconds=60)
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    db.execute("UPDATE task_queue SET lease_until=? WHERE id=?", (expired, claimed.id))
    store.recover_expired()
    queue_row = db.fetchone("SELECT status FROM task_queue WHERE id=?", (claimed.id,))
    task = db.fetchone("SELECT status,error FROM publish_tasks WHERE id=?", (task_id,))
    assert queue_row["status"] == "manual"
    assert task["status"] == "verification_pending"
    assert "不会自动重发" in task["error"]


def test_persisted_daily_request_limit(tmp_path):
    db = Database(tmp_path / "dashboard.sqlite3")
    store = P0Store(db)
    account_id = make_account(db, tmp_path, "one")
    assert store.acquire_request(account_id, 1.0, 1) == 0
    with pytest.raises(DailyLimitReached):
        store.acquire_request(account_id, 1.0, 1)


def test_publish_preflight_rejects_duplicate_content(tmp_path):
    config = DashboardConfig.load(tmp_path / "data")
    db = Database(config.database_path)
    P0Store(db)
    account_id = make_account(db, tmp_path, "one")
    image = tmp_path / "one.jpg"
    image.write_bytes(b"image")
    first = db.create_publish_task(account_id, "标题", "正文", "[]", json_dumps([str(image)]))
    second = db.create_publish_task(account_id, "标题", "正文", "[]", json_dumps([str(image)]))
    publisher = BrowserPublisher(db, config, AccountBrowserService(db, config))
    first_task = db.fetchone("SELECT * FROM publish_tasks WHERE id=?", (first,))
    account = db.fetchone("SELECT * FROM accounts WHERE id=?", (account_id,))
    publisher._preflight(first_task, account)
    db.update("publish_tasks", first, status="published")
    with pytest.raises(PublishFlowError, match="相同内容"):
        publisher._preflight(db.fetchone("SELECT * FROM publish_tasks WHERE id=?", (second,)), account)


def test_cross_origin_mutation_is_rejected(tmp_path):
    app = create_app(tmp_path / "data")
    with TestClient(app) as client:
        response = client.post("/accounts", data={"alias": "bad"}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert not app.state.db.fetchone("SELECT id FROM accounts WHERE alias='bad'")


def test_duplicate_xhs_identity_is_rejected(tmp_path):
    config = DashboardConfig.load(tmp_path / "data")
    db = Database(config.database_path)
    P0Store(db)
    first = db.create_account("one", str(config.profiles_dir / "one"))
    second = db.create_account("two", str(config.profiles_dir / "two"))
    db.update("accounts", first, xhs_user_id="same-user")
    service = AccountBrowserService(db, config)
    with pytest.raises(RuntimeError, match="已绑定"):
        service._ensure_unique_identity(second, "same-user")


def test_stale_profile_lock_is_removed(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    lock = profile / "parent.lock"
    lock.touch()
    monkeypatch.setattr(AccountBrowserService, "_profile_in_use", staticmethod(lambda _profile: False))
    monkeypatch.setattr(
        AccountBrowserService, "_remove_parent_lock", staticmethod(lambda p: (p / "parent.lock").unlink())
    )
    service = AccountBrowserService.__new__(AccountBrowserService)
    service._prepare_profile(profile)
    assert not lock.exists()


def test_active_profile_lock_is_never_removed(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    lock = profile / "parent.lock"
    lock.touch()
    monkeypatch.setattr(AccountBrowserService, "_profile_in_use", staticmethod(lambda _profile: True))
    service = AccountBrowserService.__new__(AccountBrowserService)
    with pytest.raises(AccountBrowserBusy, match="Camoufox"):
        service._prepare_profile(profile)
    assert lock.exists()


def test_same_account_browser_slot_is_non_reentrant(tmp_path):
    config = DashboardConfig.load(tmp_path / "data")
    service = AccountBrowserService(Database(config.database_path), config)
    with service._browser_slot(7):
        with pytest.raises(AccountBrowserBusy, match="already in use"):
            with service._browser_slot(7):
                pass


def test_delete_unused_account_removes_profile(tmp_path):
    app = create_app(tmp_path / "data")
    db = app.state.db
    profile = app.state.config.profiles_dir / "delete-me"
    profile.mkdir()
    account_id = db.create_account("delete-me", str(profile))
    db.update("accounts", account_id, profile_acl_status="owner_and_system")
    AccountBrowserService(db, app.state.config).delete_account(account_id)
    assert db.fetchone("SELECT id FROM accounts WHERE id=?", (account_id,)) is None
    assert not profile.exists()


def test_delete_account_with_history_is_refused(tmp_path):
    app = create_app(tmp_path / "data")
    db = app.state.db
    profile = app.state.config.profiles_dir / "keep-me"
    profile.mkdir()
    account_id = db.create_account("keep-me", str(profile))
    db.update("accounts", account_id, profile_acl_status="owner_and_system")
    db.create_search_job({"name": "history", "account_id": account_id})
    with pytest.raises(ValueError, match="related history"):
        AccountBrowserService(db, app.state.config).delete_account(account_id)
    assert db.fetchone("SELECT id FROM accounts WHERE id=?", (account_id,)) is not None
    assert profile.exists()
