import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xhs_cli.dashboard.ai import AIService, DraftOutput, SearchPlan, SearchSubTask
from xhs_cli.dashboard.app import create_app
from xhs_cli.dashboard.config import DashboardConfig
from xhs_cli.dashboard.db import Database
from xhs_cli.dashboard.importer import PublishImporter
from xhs_cli.dashboard.materials import MaterialWorkflow
from xhs_cli.dashboard.operations import OperationsStore


def make_account(db: Database, tmp_path: Path, alias: str = "brand") -> int:
    account_id = db.create_account(alias, str(tmp_path / alias))
    db.update("accounts", account_id, login_status="ready")
    return account_id


def test_unverified_material_cannot_enter_derivative_flow(tmp_path):
    config = DashboardConfig.load(tmp_path / "data")
    db = Database(config.database_path)
    store = OperationsStore(db)
    workflow = MaterialWorkflow(db, store, PublishImporter(db, config))
    source_id = workflow.register_source("网络素材", "unverified")
    with pytest.raises(ValueError, match="未确认"):
        workflow.create_candidate(note_id=None, source_id=source_id)


def test_finished_derivative_requires_rights_and_imports_pending_review(tmp_path):
    config = DashboardConfig.load(tmp_path / "data")
    db = Database(config.database_path)
    store = OperationsStore(db)
    account_id = make_account(db, tmp_path)
    workflow = MaterialWorkflow(db, store, PublishImporter(db, config))
    source_id = workflow.register_source("自有拍摄", "owned")
    candidate_id = workflow.create_candidate(note_id=None, source_id=source_id)
    derivative_id = workflow.create_derivative_task(candidate_id, "重做图文")
    final_dir = tmp_path / "finished"
    final_dir.mkdir()
    (final_dir / "001.jpg").write_bytes(b"image")
    (final_dir / "post.md").write_text(
        f"---\ntitle: 自有成品\naccount_id: {account_id}\nimages:\n  - 001.jpg\n---\n原创正文",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="拥有使用权"):
        workflow.import_finished_assets(derivative_id, final_dir, rights_declared=False)
    publish_ids = workflow.import_finished_assets(derivative_id, final_dir, rights_declared=True)
    assert len(publish_ids) == 1
    publish = db.fetchone("SELECT * FROM publish_tasks WHERE id=?", (publish_ids[0],))
    derivative = db.fetchone("SELECT * FROM derivative_tasks WHERE id=?", (derivative_id,))
    assert publish["status"] == "pending_review"
    assert derivative["rights_declared"] == 1
    assert derivative["status"] == "imported_pending_review"


class SensitiveProvider:
    name = "fake"
    last_model = "fake"

    def generate(self, instructions, payload, schema, *, complex_task=False):
        return DraftOutput(content="请加微信 hello_123", rationale="bad", risk="low")


def test_sensitive_model_output_is_discarded_before_database_write(tmp_path):
    db = Database(tmp_path / "dashboard.sqlite3")
    store = OperationsStore(db)
    run_id = store.create_agent_run("agent_draft", {"kind": "dm_reply"})
    assert AIService(store, SensitiveProvider()).run(run_id) == "failed"
    run = db.fetchone("SELECT * FROM agent_runs WHERE id=?", (run_id,))
    assert run["output_json"] == "{}"
    assert not db.fetchone("SELECT id FROM drafts")
    assert "hello_123" not in json.dumps(run, ensure_ascii=False)


def test_completed_search_brief_can_create_real_search_job(tmp_path):
    app = create_app(tmp_path / "data")
    account_id = make_account(app.state.db, tmp_path)
    run_id = app.state.extension.store.create_agent_run("search_brief", {"account_id": account_id})
    plan = SearchPlan(
        name="露营",
        subtasks=[SearchSubTask(angle="爆款结构", keywords=["露营"], topics=["户外"], media_type="image")],
    )
    app.state.db.execute(
        "UPDATE agent_runs SET status='complete',output_json=? WHERE id=?",
        (plan.model_dump_json(), run_id),
    )
    queued: list[tuple[int, int]] = []
    app.state.queue.enqueue_search = lambda job_id, selected: queued.append((job_id, selected)) or 1
    with TestClient(app) as client:
        response = client.post(
            f"/research/runs/{run_id}/create-search", data={"account_id": account_id}, follow_redirects=False
        )
    assert response.status_code == 303
    job = app.state.db.fetchone("SELECT * FROM search_jobs ORDER BY id DESC LIMIT 1")
    assert json.loads(job["keywords_json"]) == ["露营"]
    assert json.loads(job["topics_json"]) == ["户外"]
    assert job["media_type"] == "image"
    assert queued == [(job["id"], account_id)]
