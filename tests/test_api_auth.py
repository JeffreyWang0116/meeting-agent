"""API_TOKEN 設定時，/api/* 需要 Authorization: Bearer <token>。"""
import pytest
from fastapi.testclient import TestClient

from app.agents.decision_agent import DecisionAgent
from app.agents.executor_agent import ExecutorAgent
from app.agents.notifier_agent import NotifierAgent
from app.agents.parser_agent import ParserAgent
from app.config import Settings
from app.main import create_app
from app.orchestrator import Orchestrator
from app.stores.local_store import LocalJsonStore
from tests.test_decision import valid_json


@pytest.fixture
def client(tmp_path):
    settings = Settings(gemini_api_key=None, data_dir=tmp_path, api_token="secret123")
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(generate=lambda prompt: valid_json()),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(tmp_path / "notifications"),
    )
    app = create_app(settings, store=store, orchestrator=orchestrator)
    return TestClient(app)


def test_api_request_without_token_rejected(client):
    resp = client.get("/api/meetings")
    assert resp.status_code == 401


def test_api_request_with_wrong_token_rejected(client):
    resp = client.get("/api/meetings", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_api_request_with_correct_token_allowed(client):
    resp = client.get("/api/meetings", headers={"Authorization": "Bearer secret123"})
    assert resp.status_code == 200


def test_health_does_not_require_token(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200


def test_static_page_does_not_require_token(client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_no_token_configured_means_no_auth_required(tmp_path):
    settings = Settings(gemini_api_key=None, data_dir=tmp_path)
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(generate=lambda prompt: valid_json()),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(tmp_path / "notifications"),
    )
    app = create_app(settings, store=store, orchestrator=orchestrator)
    client = TestClient(app)
    resp = client.get("/api/meetings")
    assert resp.status_code == 200
