from xhs_cli.dashboard.collector import normalize_note


def test_normalize_note_payload_and_score():
    raw = {
        "items": [
            {
                "note_card": {
                    "title": "爆款",
                    "desc": "正文",
                    "type": "normal",
                    "time": 1_700_000_000_000,
                    "user": {"user_id": "u1", "nickname": "作者"},
                    "interact_info": {
                        "liked_count": "1.2万",
                        "collected_count": "500",
                        "comment_count": "100",
                        "share_count": "20",
                    },
                    "tag_list": [{"name": "美食"}],
                    "image_list": [{"url_default": "https://img/a.jpg"}],
                }
            }
        ]
    }
    note = normalize_note("n1", "token", raw, {"likes": 1, "collects": 2, "comments": 3, "shares": 1})
    assert note["author_id"] == "u1"
    assert note["likes"] == 12_000
    assert note["viral_score"] == 13_320
    assert "a.jpg" in note["images_json"]


def test_single_candidate_parse_failure_preserves_successes(tmp_path, monkeypatch):
    from xhs_cli.dashboard.collector import CollectorService
    from xhs_cli.dashboard.config import DashboardConfig
    from xhs_cli.dashboard.db import Database
    from xhs_cli.dashboard.persistence import P0Store

    config = DashboardConfig.load(tmp_path / "data")
    db = Database(config.database_path)
    store = P0Store(db)
    account_id = db.create_account("reader", str(config.profiles_dir / "reader"))
    db.update("accounts", account_id, login_status="ready", profile_acl_status="owner_and_system")
    job_id = db.create_search_job(
        {
            "name": "partial",
            "account_id": account_id,
            "keywords_json": '["camping"]',
            "min_score": 0,
            "include_comments": 0,
        }
    )

    class FakeBrowser:
        @staticmethod
        def cookies(_account_id):
            return {"a1": "test"}

    class FakeLimiter:
        @staticmethod
        def acquire(_account_id):
            return None

        @staticmethod
        def pause(_account_id, _reason):
            return None

    class FakeExporter:
        @staticmethod
        def export(_job, _note):
            return None

    class FakeClient:
        def __init__(self, _cookies):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def search_notes(*_args, **_kwargs):
            return {
                "items": [
                    {"id": "n1", "xsec_token": "t1", "note_card": {}},
                    {"id": "n2", "xsec_token": "t2", "note_card": {}},
                ],
                "has_more": False,
            }

        @staticmethod
        def get_note_detail(note_id, **_kwargs):
            if note_id == "n2":
                raise ValueError("missing initial state")
            return {"items": [{"note_card": {"title": "valid", "type": "normal"}}]}

    monkeypatch.setattr("xhs_cli.dashboard.collector.XhsClient", FakeClient)
    collector = CollectorService(db, FakeBrowser(), FakeExporter(), FakeLimiter(), store)
    assert collector.run(job_id) == "complete"
    job = db.fetchone("SELECT * FROM search_jobs WHERE id=?", (job_id,))
    assert job["status"] == "complete"
    assert job["result_count"] == 1
    assert job["progress_current"] == 2
    assert "1 candidate" in job["error"]
    states = db.fetchall("SELECT status,last_error FROM search_candidates WHERE job_id=? ORDER BY note_id", (job_id,))
    assert [row["status"] for row in states] == ["accepted", "failed"]
    assert states[1]["last_error"] == "missing initial state"
