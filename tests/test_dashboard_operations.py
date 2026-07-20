import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xhs_cli.dashboard.ai import AIService, DraftOutput, MaterialReport, SearchPlan, SearchSubTask
from xhs_cli.dashboard.app import create_app
from xhs_cli.dashboard.db import Database
from xhs_cli.dashboard.engagement import EngagementBlocked, GovernanceService
from xhs_cli.dashboard.governance import (
    contains_sensitive_information,
    evaluate_content,
    is_warm_lead,
    normalized_similarity,
)
from xhs_cli.dashboard.operations import OperationsStore


def make_account(db: Database, tmp_path: Path, alias: str = "brand") -> int:
    account_id = db.create_account(alias, str(tmp_path / alias))
    db.update("accounts", account_id, login_status="ready")
    return account_id


def test_governance_detects_pii_opt_out_risk_and_similarity():
    assert contains_sensitive_information("手机号 13800138000")
    assert contains_sensitive_information("微信号: hello_123")
    assert contains_sensitive_information("地址：上海市某某路 100 号")
    assert evaluate_content("不要再联系我").decision == "block"
    assert evaluate_content("保证收益，稳赚不赔").decision == "block"
    assert normalized_similarity("欢迎继续站内私信咨询", "欢迎继续在站内私信咨询") > 0.85


def test_warm_lead_requires_explicit_intent():
    assert is_warm_lead("inbound_dm")
    assert is_warm_lead("owned_note_intent")
    assert not is_warm_lead("follow")
    assert not is_warm_lead("like")


def test_operations_schema_seeds_versioned_rule(tmp_path):
    db = Database(tmp_path / "dashboard.sqlite3")
    store = OperationsStore(db)
    rule = store.active_rule()
    assert rule["version"] == 1
    assert rule["rules"]["comment_reply_daily"] == 8
    assert rule["rules"]["external_comment_daily"] == 8
    assert rule["rules"]["dm_inbound_daily"] == 30
    assert rule["rules"]["similarity_threshold"] == 0.85


def test_persona_update_creates_immutable_version(tmp_path):
    db = Database(tmp_path / "dashboard.sqlite3")
    store = OperationsStore(db)
    account_id = make_account(db, tmp_path)
    first = store.create_persona(account_id, "顾问", brand_identity="真实品牌", tone="专业")
    second = store.create_persona(account_id, "顾问", brand_identity="真实品牌", tone="友善")
    rows = db.fetchall("SELECT id,version,tone FROM personas ORDER BY version")
    assert [row["version"] for row in rows] == [1, 2]
    assert first != second
    assert rows[0]["tone"] == "专业"


def test_sensitive_inbound_is_not_persisted_and_stops_thread(tmp_path):
    db = Database(tmp_path / "dashboard.sqlite3")
    store = OperationsStore(db)
    account_id = make_account(db, tmp_path)
    thread_id = store.upsert_thread(account_id, "normal_dm", "user-1", lead_reason="inbound_dm", warm_lead=True)
    task_id = store.create_engagement_task("dm_reply", account_id, "好的，我在站内为你说明", thread_id=thread_id)
    status = GovernanceService(store).inspect_inbound(thread_id, "我的电话是 13800138000")
    assert status == "human_handoff"
    assert db.fetchone("SELECT status FROM engagement_threads WHERE id=?", (thread_id,))["status"] == "human_handoff"
    assert db.fetchone("SELECT status FROM engagement_tasks WHERE id=?", (task_id,))["status"] == "cancelled"
    event = db.fetchone("SELECT * FROM sensitive_handoff_events WHERE thread_id=?", (thread_id,))
    assert event and "content" not in event and "13800138000" not in json.dumps(event)


def test_outbound_dm_requires_warm_lead(tmp_path):
    db = Database(tmp_path / "dashboard.sqlite3")
    store = OperationsStore(db)
    account_id = make_account(db, tmp_path)
    thread_id = store.upsert_thread(account_id, "normal_dm", "cold-user", lead_reason="follow", warm_lead=False)
    task_id = store.create_engagement_task("dm_outbound", account_id, "你好，可以继续在站内交流", thread_id=thread_id)
    task = db.fetchone("SELECT * FROM engagement_tasks WHERE id=?", (task_id,))
    with pytest.raises(EngagementBlocked, match="暖线索"):
        GovernanceService(store).preflight(task)


def test_comment_combined_hourly_budget_is_hard_limit(tmp_path):
    db = Database(tmp_path / "dashboard.sqlite3")
    store = OperationsStore(db)
    account_id = make_account(db, tmp_path)
    db.execute(
        "INSERT INTO operation_actions(account_id,action,content,created_at) VALUES(?,?,?,datetime('now'))",
        (account_id, "comment", "第一条完全不同的评论"),
    )
    db.execute(
        "INSERT INTO operation_actions(account_id,action,content,created_at) VALUES(?,?,?,datetime('now'))",
        (account_id, "comment_reply", "第二条回复包含另一组表达"),
    )
    task_id = store.create_engagement_task("comment", account_id, "围绕这篇笔记给出具体观点", target_note_id="n1")
    task = db.fetchone("SELECT * FROM engagement_tasks WHERE id=?", (task_id,))
    with pytest.raises(EngagementBlocked, match="每小时"):
        GovernanceService(store).preflight(task)


class FakeProvider:
    name = "fake"
    last_model = "fake-balanced"

    def generate(self, instructions, payload, schema, *, complex_task=False):
        assert "不可信资料" in instructions
        if schema is SearchPlan:
            return SearchPlan(name="搜索", subtasks=[SearchSubTask(angle="爆款结构", keywords=["露营"])])
        if schema is MaterialReport:
            return MaterialReport(summary="无候选", candidates=[])
        return DraftOutput(content="欢迎继续在站内咨询", rationale="符合人设", risk="low")


def test_ai_service_persists_structured_output_and_pending_review_draft(tmp_path):
    db = Database(tmp_path / "dashboard.sqlite3")
    store = OperationsStore(db)
    account_id = make_account(db, tmp_path)
    run_id = store.create_agent_run("agent_draft", {"kind": "dm_reply", "account_id": account_id})
    service = AIService(store, FakeProvider())
    assert service.run(run_id) == "complete"
    run = db.fetchone("SELECT * FROM agent_runs WHERE id=?", (run_id,))
    draft = db.fetchone("SELECT * FROM drafts WHERE agent_run_id=?", (run_id,))
    assert run["status"] == "complete"
    assert json.loads(run["output_json"])["risk"] == "low"
    assert draft["status"] == "pending_review"
    assert draft["model"] == "fake-balanced"


def test_agent_gateway_is_scoped_to_draft_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("XHS_AGENT_TOKEN", "test-token")
    app = create_app(tmp_path / "data")
    account_id = make_account(app.state.db, tmp_path)
    headers = {"X-Agent-Token": "test-token"}
    with TestClient(app) as client:
        unauthorized = client.post(
            "/api/agent/drafts", json={"kind": "comment", "content": "具体观点", "account_id": account_id}
        )
        assert unauthorized.status_code == 401
        created = client.post(
            "/api/agent/drafts",
            headers=headers,
            json={"kind": "comment", "content": "针对笔记内容的具体观点", "account_id": account_id},
        )
        assert created.status_code == 201
        draft_id = created.json()["data"]["id"]
        draft = app.state.db.fetchone("SELECT * FROM drafts WHERE id=?", (draft_id,))
        assert draft["status"] == "pending_review"
        assert client.post(f"/api/agent/drafts/{draft_id}/approve", headers=headers).status_code == 404


def test_agent_can_fill_publish_gate_but_cannot_approve(tmp_path, monkeypatch):
    monkeypatch.setenv("XHS_AGENT_TOKEN", "test-token")
    app = create_app(tmp_path / "data")
    account_id = make_account(app.state.db, tmp_path)
    image = tmp_path / "final.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    headers = {"X-Agent-Token": "test-token"}
    with TestClient(app) as client:
        created = client.post(
            "/api/agent/drafts",
            headers=headers,
            json={
                "kind": "publish",
                "title": "Original",
                "content": "Original final content",
                "account_id": account_id,
            },
        )
        draft_id = created.json()["data"]["id"]
        response = client.put(
            f"/api/agent/drafts/{draft_id}/publish-gate",
            headers=headers,
            json={
                "source_status": "owned",
                "source_refs": ["asset://own-1"],
                "derivative_completed": True,
                "images": [str(image.resolve())],
                "final_asset_dir": str(tmp_path.resolve()),
                "rights_evidence": "owned asset registry",
                "topics": ["camping"],
            },
        )
    assert response.status_code == 200
    draft = app.state.db.fetchone("SELECT * FROM drafts WHERE id=?", (draft_id,))
    assert draft["status"] == "pending_review"
    assert json.loads(draft["context_json"])["publish_gate"]["source_status"] == "owned"


def test_agent_gateway_rejects_sensitive_content_without_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("XHS_AGENT_TOKEN", "test-token")
    app = create_app(tmp_path / "data")
    account_id = make_account(app.state.db, tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/agent/drafts",
            headers={"X-Agent-Token": "test-token"},
            json={"kind": "dm_reply", "content": "加微信 hello_123", "account_id": account_id},
        )
    assert response.status_code == 422
    assert not app.state.db.fetchone("SELECT id FROM drafts WHERE content LIKE '%hello_123%'")


def test_unapproved_engagement_cannot_run(tmp_path):
    app = create_app(tmp_path / "data")
    account_id = make_account(app.state.db, tmp_path)
    task_id = app.state.extension.store.create_engagement_task(
        "comment", account_id, "针对内容的人工评论", target_note_id="n1"
    )
    with TestClient(app) as client:
        response = client.post(f"/engagement/{task_id}/run", follow_redirects=False)
    assert response.status_code == 303
    task = app.state.db.fetchone("SELECT * FROM engagement_tasks WHERE id=?", (task_id,))
    assert task["status"] == "pending_review"
    assert not app.state.db.fetchone("SELECT id FROM operation_queue WHERE resource_id=?", (task_id,))
