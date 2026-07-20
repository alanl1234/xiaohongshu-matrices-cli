from fastapi.testclient import TestClient

from xhs_cli.dashboard.app import create_app


def test_dashboard_and_health_endpoint(tmp_path):
    app = create_app(tmp_path / "data")
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "运营总览" in response.text
        health = client.get("/api/health").json()
        assert health["ok"] is True


def test_unapproved_task_cannot_enter_publish_queue(tmp_path):
    app = create_app(tmp_path / "data")
    db = app.state.db
    account_id = db.create_account("a", str(tmp_path / "profile"))
    task_id = db.create_publish_task(account_id, "标题", "正文", "[]", '["image.jpg"]')
    with TestClient(app) as client:
        response = client.post(f"/publish/{task_id}/run", follow_redirects=False)
        assert response.status_code == 303
    assert db.fetchone("SELECT status FROM publish_tasks WHERE id=?", (task_id,))["status"] == "pending_review"
