"""Publisher preflight validation and state-machine tests (no browser)."""

import json
from pathlib import Path

import pytest

from xhs_cli.dashboard.browser import AccountBrowserService
from xhs_cli.dashboard.config import DashboardConfig
from xhs_cli.dashboard.db import Database
from xhs_cli.dashboard.publisher import BrowserPublisher, PublishFlowError


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


@pytest.fixture
def cfg(tmp_path):
    return DashboardConfig.load(tmp_path)


@pytest.fixture
def browsers(db, cfg):
    return AccountBrowserService(db, cfg)


@pytest.fixture
def publisher(db, cfg, browsers):
    return BrowserPublisher(db, cfg, browsers)


def _make_account(db, tmp: Path, alias: str = "test_acct") -> int:
    aid = db.create_account(alias, str(tmp / alias))
    db.update("accounts", aid, login_status="ready")
    return aid


def _make_task(db, account_id: int, title: str = "测试标题", body: str = "测试正文",
               images: list[str] | None = None, **overrides) -> int:
    task_id = db.create_publish_task(
        account_id=account_id,
        title=title,
        body=body,
        topics_json="[]",
        images_json=json.dumps(images or []),
        source_dir="",
    )
    if images:
        db.update("publish_tasks", task_id, images_json=json.dumps(images))
    for k, v in overrides.items():
        db.update("publish_tasks", task_id, **{k: v})
    return task_id


# ── fingerprint ──────────────────────────────────────────────────────────

def test_fingerprint_changes_with_title(publisher, tmp_path):
    img = tmp_path / "img.png"
    img.write_text("img_data")
    task = {"account_id": 1, "title": "A", "body": "B"}
    fp1 = publisher._fingerprint(task, [img])
    task["title"] = "C"
    fp2 = publisher._fingerprint(task, [img])
    assert fp1 != fp2


def test_fingerprint_changes_with_images(publisher, tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_text("img_a")
    b.write_text("img_b")
    task = {"account_id": 1, "title": "X", "body": "Y"}
    assert publisher._fingerprint(task, [a]) != publisher._fingerprint(task, [b])


def test_fingerprint_idempotent(publisher, tmp_path):
    img = tmp_path / "img.png"
    img.write_text("hello")
    task = {"account_id": 1, "title": "T", "body": "B"}
    assert publisher._fingerprint(task, [img]) == publisher._fingerprint(task, [img])


# ── preflight ────────────────────────────────────────────────────────────

def test_preflight_rejects_empty_title(db, publisher, tmp_path):
    aid = _make_account(db, tmp_path)
    tid = _make_task(db, aid, title="", body="正文")
    task = db.fetchone("SELECT * FROM publish_tasks WHERE id=?", (tid,))
    with pytest.raises(PublishFlowError, match="标题"):
        publisher._preflight(task, db.fetchone("SELECT * FROM accounts WHERE id=?", (aid,)))


def test_preflight_rejects_empty_body(db, publisher, tmp_path):
    aid = _make_account(db, tmp_path)
    tid = _make_task(db, aid, title="标题", body="")
    task = db.fetchone("SELECT * FROM publish_tasks WHERE id=?", (tid,))
    with pytest.raises(PublishFlowError, match="正文"):
        publisher._preflight(task, db.fetchone("SELECT * FROM accounts WHERE id=?", (aid,)))


def test_preflight_rejects_title_too_long(db, publisher, tmp_path):
    aid = _make_account(db, tmp_path)
    img = tmp_path / "ok.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    tid = _make_task(db, aid, title="X" * 25, images=[str(img)])
    task = db.fetchone("SELECT * FROM publish_tasks WHERE id=?", (tid,))
    with pytest.raises(PublishFlowError, match="标题"):
        publisher._preflight(task, db.fetchone("SELECT * FROM accounts WHERE id=?", (aid,)))


def test_preflight_rejects_body_plus_topics_too_long(db, publisher, tmp_path):
    aid = _make_account(db, tmp_path)
    tid = _make_task(db, aid, title="标题", body="正" * 990)
    db.update("publish_tasks", tid, topics_json=json.dumps(["#" + str(i) for i in range(20)]))
    task = db.fetchone("SELECT * FROM publish_tasks WHERE id=?", (tid,))
    with pytest.raises(PublishFlowError, match="1000"):
        publisher._preflight(task, db.fetchone("SELECT * FROM accounts WHERE id=?", (aid,)))


def test_preflight_rejects_zero_images(db, publisher, tmp_path):
    aid = _make_account(db, tmp_path)
    tid = _make_task(db, aid, images=[])
    task = db.fetchone("SELECT * FROM publish_tasks WHERE id=?", (tid,))
    with pytest.raises(PublishFlowError, match="1.*18"):
        publisher._preflight(task, db.fetchone("SELECT * FROM accounts WHERE id=?", (aid,)))


def test_preflight_rejects_too_many_images(db, publisher, tmp_path):
    aid = _make_account(db, tmp_path)
    tid = _make_task(db, aid, images=[str(tmp_path / f"{i}.png") for i in range(20)])
    task = db.fetchone("SELECT * FROM publish_tasks WHERE id=?", (tid,))
    with pytest.raises(PublishFlowError, match="1.*18"):
        publisher._preflight(task, db.fetchone("SELECT * FROM accounts WHERE id=?", (aid,)))


def test_preflight_rejects_nonexistent_image(db, publisher, tmp_path):
    aid = _make_account(db, tmp_path)
    tid = _make_task(db, aid, images=[str(tmp_path / "ghost.png")])
    task = db.fetchone("SELECT * FROM publish_tasks WHERE id=?", (tid,))
    with pytest.raises(PublishFlowError, match="不存在|不支持"):
        publisher._preflight(task, db.fetchone("SELECT * FROM accounts WHERE id=?", (aid,)))


def test_preflight_rejects_unsupported_suffix(db, publisher, tmp_path):
    aid = _make_account(db, tmp_path)
    bad = tmp_path / "img.bmp"
    bad.write_bytes(b"\x00\x00\x00\x00")
    tid = _make_task(db, aid, images=[str(bad)])
    task = db.fetchone("SELECT * FROM publish_tasks WHERE id=?", (tid,))
    with pytest.raises(PublishFlowError):
        publisher._preflight(task, db.fetchone("SELECT * FROM accounts WHERE id=?", (aid,)))


def test_preflight_rejects_huge_image(db, publisher, tmp_path):
    aid = _make_account(db, tmp_path)
    huge = tmp_path / "huge.png"
    with huge.open("wb") as f:
        f.seek(21 * 1024 * 1024 - 1)
        f.write(b"\x00")
    tid = _make_task(db, aid, images=[str(huge)])
    task = db.fetchone("SELECT * FROM publish_tasks WHERE id=?", (tid,))
    with pytest.raises(PublishFlowError, match="20MB"):
        publisher._preflight(task, db.fetchone("SELECT * FROM accounts WHERE id=?", (aid,)))


def test_preflight_accepts_valid_payload(db, publisher, tmp_path):
    aid = _make_account(db, tmp_path)
    img = tmp_path / "ok.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    tid = _make_task(db, aid, images=[str(img)])
    task = db.fetchone("SELECT * FROM publish_tasks WHERE id=?", (tid,))
    images, body = publisher._preflight(task, db.fetchone("SELECT * FROM accounts WHERE id=?", (aid,)))
    assert len(images) == 1
    assert "测试正文" in body


def test_preflight_rejects_account_not_ready(db, publisher, tmp_path):
    aid = _make_account(db, tmp_path, alias="pending")
    db.update("accounts", aid, login_status="needs_login")
    img = tmp_path / "ok.png"
    img.write_bytes(b"\x89PNG")
    tid = _make_task(db, aid, images=[str(img)])
    task = db.fetchone("SELECT * FROM publish_tasks WHERE id=?", (tid,))
    with pytest.raises(PublishFlowError, match="登录"):
        publisher._preflight(task, db.fetchone("SELECT * FROM accounts WHERE id=?", (aid,)))


def test_extract_notes_handles_empty():
    assert BrowserPublisher._extract_notes({}) == []
    assert BrowserPublisher._extract_notes({"notes": None}) == []


def test_extract_notes_from_list():
    data = {"note_list": [{"note_id": "n1"}, {"title": "t"}]}
    assert len(BrowserPublisher._extract_notes(data)) == 2


def test_note_id_fallbacks():
    assert BrowserPublisher._note_id({"note_id": "abc"}) == "abc"
    assert BrowserPublisher._note_id({"id": "123"}) == "123"
    assert BrowserPublisher._note_id({"noteId": "xyz"}) == "xyz"
    assert BrowserPublisher._note_id({}) == ""


# ── cooldown ─────────────────────────────────────────────────────────────

def test_check_cooldown_passes_when_no_last_publish(db, publisher, tmp_path):
    aid = _make_account(db, tmp_path)
    publisher._check_cooldown(db.fetchone("SELECT * FROM accounts WHERE id=?", (aid,)))


def test_check_cooldown_passes_when_expired(db, publisher, tmp_path):
    aid = _make_account(db, tmp_path)
    from datetime import UTC, datetime, timedelta
    db.update("accounts", aid, last_publish_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat())
    # default cooldown is typically < 2 hours
    publisher._check_cooldown(db.fetchone("SELECT * FROM accounts WHERE id=?", (aid,)))


def test_run_rejects_non_approved_task(db, publisher, tmp_path):
    aid = _make_account(db, tmp_path)
    tid = _make_task(db, aid)
    # default status is pending_review
    result = publisher.run(tid)
    assert result != "published"
