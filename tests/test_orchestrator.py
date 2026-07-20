"""Orchestrator 受治理全自动编排的单测（无网络、无浏览器）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xhs_cli.dashboard.ai import (
    MaterialInsight,
    MaterialReport,
    ScreenedNote,
    ScreenReport,
    SearchPlan,
    SearchSubTask,
)
from xhs_cli.dashboard.app import create_app
from xhs_cli.dashboard.db import Database


def make_account(db: Database, tmp_path: Path, alias: str = "brand") -> int:
    account_id = db.create_account(alias, str(tmp_path / alias))
    db.update("accounts", account_id, login_status="ready")
    return account_id


def write_goals(data_dir: Path, goals: list[dict]) -> None:
    (data_dir / "orchestrator_goals.json").write_text(json.dumps(goals), encoding="utf-8")


def set_run_complete(db: Database, run_id: int, output: str) -> None:
    db.execute("UPDATE agent_runs SET status='complete', output_json=? WHERE id=?", (output, run_id))


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("XHS_ORCHESTRATOR", "1")
    monkeypatch.setenv("XHS_ENGAGEMENT_MODE", "reviewed")
    monkeypatch.setenv("XHS_AUTO_PUBLISH", "approve")
    pool = tmp_path / "pool"
    pool.mkdir()
    for i in range(3):
        (pool / f"img{i}.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    monkeypatch.setenv("XHS_ASSET_POOL_DIR", str(pool))
    return create_app(tmp_path / "data")


def test_orchestrator_disabled_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("XHS_ORCHESTRATOR", raising=False)
    app = create_app(tmp_path / "data")
    assert app.state.orchestrator.enabled is False
    app.state.orchestrator.start()  # no-op
    assert app.state.orchestrator._thread is None


def test_dispatch_goals_creates_search_brief(app):
    orch = app.state.orchestrator
    write_goals(orch.config.data_dir, [{"id": "g1", "objective": "露营选题", "cadence_hours": 24}])
    orch._dispatch_goals(orch._load_goals())
    runs = app.state.db.fetchall("SELECT * FROM agent_runs WHERE kind='search_brief'")
    assert len(runs) == 1
    # 同一轮不应重复派发（已记 marker）
    orch._dispatch_goals(orch._load_goals())
    assert len(app.state.db.fetchall("SELECT * FROM agent_runs WHERE kind='search_brief'")) == 1


def test_pipeline_chaining_search_brief_to_draft(app):
    orch = app.state.orchestrator
    db = app.state.db
    make_account(db, orch.config.data_dir.parent)
    store = app.state.extension.store

    # 1) search_brief 完成 -> 每个子任务一个采集任务
    run_id = store.create_agent_run(
        "search_brief", {"objective": "露营", "max_candidates": 8}
    )
    set_run_complete(
        db,
        run_id,
        SearchPlan(
            name="露营",
            subtasks=[
                SearchSubTask(angle="爆款结构", keywords=["露营"], criteria="收藏明显高于点赞"),
                SearchSubTask(angle="用户痛点", keywords=["露营 踩坑"], criteria="评论区高频抱怨"),
            ],
        ).model_dump_json(),
    )
    orch._advance_pipeline()
    jobs = db.fetchall("SELECT * FROM search_jobs WHERE name LIKE '露营%'")
    assert len(jobs) == 2
    for job in jobs:
        assert job["status"] == "pending"
        assert orch._marked(f"orch_job:{job['id']}")
    assert orch._marked(f"brief_done:{run_id}")

    # 2) 采集任务完成 + 笔记 -> screen_results（按角度筛选，而非热度前 N）
    job = jobs[0]
    note_id = db.upsert_note(
        {
            "note_id": "n1",
            "author_id": "a1",
            "author_name": "作者",
            "title": "露营清单",
            "body": "内容",
            "published_at": None,
            "media_type": "image",
            "original_url": "https://www.xiaohongshu.com/explore/n1",
            "likes": 0,
            "collects": 0,
            "comments": 0,
            "shares": 0,
            "viral_score": 9000,
            "topics_json": "[]",
            "images_json": "[]",
            "comments_json": "[]",
            "xsec_token": "",
            "xsec_source": "pc_search",
            "raw_json": "{}",
        }
    )
    db.link_job_note(job["id"], note_id)
    db.update("search_jobs", job["id"], status="complete")
    orch._advance_pipeline()
    screen = db.fetchone("SELECT * FROM agent_runs WHERE kind='screen_results' ORDER BY id DESC")
    assert screen and screen["status"] in {"pending", "queued"}

    # 3) screen_results 完成 -> 仅对选中笔记做 material_research
    set_run_complete(
        db,
        screen["id"],
        ScreenReport(
            summary="x",
            selections=[ScreenedNote(note_id="n1", selected=True, relevance_score=0.9, reason="命中筛选标准")],
        ).model_dump_json(),
    )
    orch._advance_pipeline()
    research = db.fetchone("SELECT * FROM agent_runs WHERE kind='material_research' ORDER BY id DESC")
    assert research and research["status"] in {"pending", "queued"}

    # 4) material_research 完成 -> agent_draft（按 max_candidates 配额，不再硬编码前 3）
    set_run_complete(
        db,
        research["id"],
        MaterialReport(
            summary="x",
            candidates=[
                MaterialInsight(
                    note_id="n1",
                    relevance_score=0.9,
                    cluster="c",
                    hook="h",
                    structure=["a"],
                    audience_pains=["p"],
                    comment_insights=["c"],
                    derivative_angles=["做露营清单"],
                    forbidden_reuse=["y"],
                ),
                MaterialInsight(
                    note_id="n2",
                    relevance_score=0.8,
                    cluster="c",
                    hook="h",
                    structure=["a"],
                    audience_pains=["p"],
                    comment_insights=["c"],
                    derivative_angles=["做城市露营"],
                    forbidden_reuse=["y"],
                ),
            ],
        ).model_dump_json(),
    )
    orch._advance_pipeline()
    drafts_runs = db.fetchall("SELECT * FROM agent_runs WHERE kind='agent_draft'")
    assert len(drafts_runs) == 2


def publish_gate(image: Path) -> dict:
    return {
        "publish_gate": {
            "source_status": "owned",
            "source_refs": ["asset://campaign-1"],
            "derivative_completed": True,
            "images": [str(image.resolve())],
            "final_asset_dir": str(image.parent.resolve()),
            "rights_evidence": "owned asset registry entry 1",
        }
    }


def test_gate_complete_draft_becomes_pending_review(app):
    orch = app.state.orchestrator
    db = app.state.db
    account_id = make_account(db, orch.config.data_dir.parent)
    store = app.state.extension.store
    image = orch.config.data_dir / "final.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    store.create_draft(
        "publish", "Original final content", account_id=account_id, title="Gate complete", context=publish_gate(image)
    )
    orch._auto_publish()
    task = db.fetchone("SELECT * FROM publish_tasks WHERE title='Gate complete'")
    assert task and task["status"] == "pending_review"
    assert json.loads(task["images_json"]) == [str(image.resolve())]
    assert not orch._marked(f"pub_at:{task['id']}")


def test_incomplete_gate_remains_retryable(app):
    orch = app.state.orchestrator
    db = app.state.db
    account_id = make_account(db, orch.config.data_dir.parent)
    store = app.state.extension.store
    draft_id = store.create_draft("publish", "Original content", account_id=account_id, title="Gate missing")
    orch._auto_publish()
    assert db.fetchone("SELECT * FROM publish_tasks WHERE title='Gate missing'") is None
    assert not orch._marked(f"draft_pub:{draft_id}")


def test_auto_engage_promotes_approved_tasks_in_reviewed_mode(app):
    orch = app.state.orchestrator
    db = app.state.db
    account_id = make_account(db, orch.config.data_dir.parent)
    store = app.state.extension.store

    task_id = store.create_engagement_task("comment", account_id, "针对内容的具体观点", target_note_id="n1")
    store.approve_task(task_id)
    orch._auto_engage()

    task = db.fetchone("SELECT * FROM engagement_tasks WHERE id=?", (task_id,))
    assert task["status"] == "queued"
    queued = db.fetchone("SELECT * FROM operation_queue WHERE resource_id=? AND kind='comment'", (task_id,))
    assert queued is not None


def test_auto_engage_does_nothing_in_shadow_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("XHS_ORCHESTRATOR", "1")
    monkeypatch.setenv("XHS_ENGAGEMENT_MODE", "shadow")
    app = create_app(tmp_path / "data")
    db = app.state.db
    account_id = make_account(db, tmp_path)
    store = app.state.extension.store
    task_id = store.create_engagement_task("comment", account_id, "具体观点", target_note_id="n1")
    store.approve_task(task_id)
    app.state.orchestrator._auto_engage()
    assert db.fetchone("SELECT status FROM engagement_tasks WHERE id=?", (task_id,))["status"] == "approved"


def test_status_endpoint_reports_enabled(app):
    client_app = app
    status = client_app.state.orchestrator
    assert status.enabled is True
    assert status.mode == "reviewed"
    assert status.auto_approve is False
