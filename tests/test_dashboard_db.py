from xhs_cli.dashboard.db import Database
from xhs_cli.dashboard.utils import json_dumps


def test_publish_state_starts_pending_review(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    account_id = db.create_account("a", str(tmp_path / "profile"))
    task_id = db.create_publish_task(account_id, "标题", "正文", "[]", json_dumps(["a.jpg"]))
    task = db.fetchone("SELECT * FROM publish_tasks WHERE id=?", (task_id,))
    assert task["status"] == "pending_review"
    assert task["attempts"] == 0


def test_note_upsert_deduplicates_by_note_id(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    note = {
        "note_id": "abc",
        "author_id": "u",
        "author_name": "n",
        "title": "t",
        "body": "b",
        "published_at": None,
        "media_type": "image",
        "original_url": "url",
        "likes": 1,
        "collects": 2,
        "comments": 3,
        "shares": 4,
        "viral_score": 18,
        "topics_json": "[]",
        "images_json": "[]",
        "comments_json": "[]",
        "xsec_token": "",
        "xsec_source": "pc_search",
        "raw_json": "{}",
    }
    first = db.upsert_note(note)
    note["likes"] = 9
    second = db.upsert_note(note)
    assert first == second
    assert db.fetchone("SELECT likes FROM notes WHERE id=?", (first,))["likes"] == 9
