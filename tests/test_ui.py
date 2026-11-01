import re

from fastapi.testclient import TestClient

import app.main as main


def test_ui_page_is_served(client):
    res = client.get("/ui")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/html")
    assert "Transaction Webhook Service" in res.text and "/v1/webhooks/transactions" in res.text


def test_ui_page_uses_no_external_resources_and_no_innerhtml(client):
    html = client.get("/ui").text
    assert not re.search(r"(src|href)=[\"']https?://", html)  # works offline, nothing third-party
    assert "innerHTML" not in html  # user-supplied text is rendered with textContent only (no XSS)


def test_ui_not_in_api_docs_and_root_health_unchanged(client):
    assert "/ui" not in client.get("/openapi.json").json()["paths"]
    assert client.get("/").json()["status"] == "HEALTHY"  # the assessment's health endpoint is still JSON


def test_ui_can_be_disabled(monkeypatch, settings):
    cfg = settings.model_copy(update={"enable_ui": False})
    monkeypatch.setattr(main, "get_settings", lambda: cfg)
    with TestClient(main.create_app()) as c:
        assert c.get("/ui").status_code == 404


def test_openapi_documents_webhook_body_and_errors(client):
    op = client.get("/openapi.json").json()["paths"]["/v1/webhooks/transactions"]["post"]
    assert set(op["responses"]) >= {"202", "400", "401", "413", "422", "503"}
    assert "example" in op["requestBody"]["content"]["application/json"]
    assert op["tags"] == ["Webhooks"]
