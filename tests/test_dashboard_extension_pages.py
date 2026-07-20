from fastapi.testclient import TestClient

from xhs_cli.dashboard.app import create_app


def test_extension_pages_render(tmp_path):
    app = create_app(tmp_path / "data")
    with TestClient(app) as client:
        for path in ("/personas", "/research", "/materials", "/engagement", "/rules"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)


def test_agent_gateway_not_enabled_without_token(tmp_path, monkeypatch):
    monkeypatch.delenv("XHS_AGENT_TOKEN", raising=False)
    app = create_app(tmp_path / "data")
    with TestClient(app) as client:
        response = client.post("/api/agent/runs", json={"kind": "search_brief", "payload": {"objective": "露营"}})
    assert response.status_code == 503
