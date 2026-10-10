"""訪客登入（Firebase 匿名登入）：可用全部功能，離開時刪資料，速率限制依 IP、額度較緊。

假登入：tok-guest1 → 匿名帳號 uid-guest1（沒有信箱）；tok-amy → Google 帳號。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.decision_agent import DecisionAgent
from app.agents.executor_agent import ExecutorAgent
from app.agents.notifier_agent import NotifierAgent
from app.agents.parser_agent import ParserAgent
from app.auth import AuthError
from app.config import Settings
from app.main import create_app
from app.orchestrator import Orchestrator
from app.stores.local_store import LocalJsonStore
from tests.test_decision import valid_json


def fake_verify(id_token: str) -> dict:
    if not id_token.startswith("tok-") or not id_token[4:]:
        raise AuthError("登入憑證無效或已過期")
    name = id_token[4:]
    if name.startswith("guest"):
        return {"uid": f"uid-{name}", "email": None, "guest": True}
    return {"uid": f"uid-{name}", "email": f"{name}@gmail.com", "guest": False}


def make_client(tmp_path, **extra):
    settings = Settings(
        gemini_api_key=None, data_dir=tmp_path,
        firebase_web_api_key="web-key", firebase_auth_domain="demo.firebaseapp.com", firebase_project_id="demo",
        **extra,
    )
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(generate=lambda p: valid_json()),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(tmp_path / "notifications"),
    )
    client = TestClient(create_app(settings, store=store, orchestrator=orchestrator, verify_token=fake_verify))
    client.store = store
    return client


@pytest.fixture
def client(tmp_path):
    return make_client(tmp_path)


def h(name, ip=None, workspace=None):
    headers = {"Authorization": f"Bearer tok-{name}"}
    if ip:
        headers["X-Forwarded-For"] = ip
    if workspace:
        headers["X-Workspace"] = workspace
    return headers


def analyze(client, who, ip=None):
    return client.post("/api/meetings", json={"text": "志明下週一交 prompt"}, headers=h(who, ip))


def test_auth_config_offers_guest_login(tmp_path):
    assert make_client(tmp_path).get("/api/auth/config").json()["guestEnabled"] is True
    assert make_client(tmp_path / "off", guest_login_enabled=False).get("/api/auth/config").json()["guestEnabled"] is False


def test_guest_can_use_the_app_with_their_own_data(client):
    assert analyze(client, "guest1").status_code == 200
    assert len(client.get("/api/meetings", headers=h("guest1")).json()["meetings"]) == 1
    assert client.get("/api/meetings", headers=h("guest2")).json()["meetings"] == []


def test_guest_tokens_rejected_when_guest_login_is_off(tmp_path):
    client = make_client(tmp_path, guest_login_enabled=False)
    assert client.get("/api/meetings", headers=h("guest1")).status_code == 403
    assert client.get("/api/meetings", headers=h("amy")).status_code == 200


def test_leaving_deletes_all_guest_data(client):
    analyze(client, "guest1")
    client.post("/api/tasks", json={"task": "手動任務"}, headers=h("guest1"))
    client.put("/api/glossary", json={"terms": [{"term": "訪客詞", "note": ""}]}, headers=h("guest1"))
    analyze(client, "amy")

    resp = client.delete("/api/guest/data", headers=h("guest1"))
    assert resp.status_code == 200
    scope = "uid-guest1"
    assert client.store.list_meetings(user=scope) == []
    assert client.store.list_tasks(user=scope) == []
    assert client.store.get_glossary(user=scope) == []
    assert len(client.get("/api/meetings", headers=h("amy")).json()["meetings"]) == 1  # 別人的不受影響


def test_only_guests_can_wipe_their_data(client):
    analyze(client, "amy")
    assert client.delete("/api/guest/data", headers=h("amy")).status_code == 403
    assert len(client.get("/api/meetings", headers=h("amy")).json()["meetings"]) == 1


def test_guests_cannot_use_groups(client):
    assert client.post("/api/groups", json={"name": "x"}, headers=h("guest1")).status_code == 403
    assert client.get("/api/groups", headers=h("guest1")).status_code == 403


def test_guest_rate_limit_is_per_ip_and_tighter(tmp_path):
    client = make_client(tmp_path, rate_limit_enabled=True, guest_rate_limits="analyze=10/1")
    assert analyze(client, "guest1", ip="1.1.1.1").status_code == 200
    # 換一個匿名帳號（清掉瀏覽器資料就有）也是同一個 IP，額度不會重來
    assert analyze(client, "guest2", ip="1.1.1.1").status_code == 429
    assert analyze(client, "guest3", ip="2.2.2.2").status_code == 200
    # 登入使用者照舊用自己的額度
    assert analyze(client, "amy", ip="1.1.1.1").status_code == 200


def test_client_ip_uses_the_last_forwarded_hop(tmp_path):
    """X-Forwarded-For 的前段由用戶端自己填、可以偽造；最後一段才是反向代理看到的來源。"""
    client = make_client(tmp_path, rate_limit_enabled=True, guest_rate_limits="analyze=10/1")
    assert analyze(client, "guest1", ip="9.9.9.9, 1.1.1.1").status_code == 200
    assert analyze(client, "guest2", ip="8.8.8.8, 1.1.1.1").status_code == 429
