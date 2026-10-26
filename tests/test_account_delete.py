"""刪除帳號：把這個人的個人資料全部刪掉、退出所有群組、刪掉 Firebase 帳號。

假登入同 test_guest：tok-amy → uid-amy（amy@gmail.com）。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.agents.decision_agent import DecisionAgent
from app.config import Settings
from app.main import create_app
from app.orchestrator import Orchestrator
from app.stores.local_store import LocalJsonStore
from tests.test_decision import valid_json
from tests.test_guest import fake_verify, h


def make_client(tmp_path, deleted=None):
    settings = Settings(
        gemini_api_key=None, data_dir=tmp_path,
        firebase_web_api_key="web-key", firebase_auth_domain="demo.firebaseapp.com", firebase_project_id="demo",
    )
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        decision=DecisionAgent(generate=lambda p: valid_json()),
        store=store,
    )
    client = TestClient(create_app(
        settings, store=store, orchestrator=orchestrator, verify_token=fake_verify,
        delete_auth_user=(deleted.append if deleted is not None else None),
    ))
    client.store = store
    return client


def analyze(client, who, workspace=None):
    return client.post("/api/meetings", json={"text": "志明下週一交 prompt"}, headers=h(who, workspace=workspace))


def make_group(client, owner, name="專題"):
    return client.post("/api/groups", json={"name": name}, headers=h(owner)).json()["id"]


def test_deletes_all_personal_data_and_the_firebase_account(tmp_path):
    deleted = []
    client = make_client(tmp_path, deleted)
    analyze(client, "amy")
    client.post("/api/tasks", json={"task": "手動任務"}, headers=h("amy"))
    client.put("/api/glossary", json={"terms": [{"term": "專有名詞", "note": ""}]}, headers=h("amy"))
    analyze(client, "bob")

    resp = client.delete("/api/account", headers=h("amy"))

    assert resp.status_code == 200
    assert client.store.list_meetings(user="uid-amy") == []
    assert client.store.list_tasks(user="uid-amy") == []
    assert client.store.get_glossary(user="uid-amy") == []
    assert deleted == ["uid-amy"]
    assert len(client.store.list_meetings(user="uid-bob")) == 1  # 別人的不受影響


def test_also_deletes_templates_feedback_and_job_records(tmp_path):
    """範本、AI 錯誤回報（含使用者寫的說明）、轉錄工作紀錄（含逐字稿）都是個人資料。"""
    client = make_client(tmp_path, [])
    mid = analyze(client, "amy").json()["meeting_id"]
    client.post("/api/templates", json={"name": "我的範本"}, headers=h("amy"))
    client.post("/api/feedback", json={
        "meeting_id": mid, "target": "todo", "reason": "other", "note": "私人說明"}, headers=h("amy"))
    client.store.save_job({"id": "j1", "user": "uid-amy", "transcript": "逐字稿"})
    client.store.save_push_subscription({"id": "p1", "user": "uid-amy", "endpoint": "https://x/1"})
    client.post("/api/templates", json={"name": "bob 的範本"}, headers=h("bob"))

    assert client.delete("/api/account", headers=h("amy")).status_code == 200

    assert client.store.list_templates("uid-amy") == []
    assert client.store.list_feedback("uid-amy") == []
    assert client.store.list_jobs("uid-amy") == []
    # 不刪的話那支手機每天還是會收到提醒，而資料已經不在了
    assert client.store.list_push_subscriptions("uid-amy") == []
    assert len(client.store.list_templates("uid-bob")) == 1  # 別人的不受影響


def test_guest_wipe_also_deletes_templates_feedback_and_job_records(tmp_path):
    from app.groups import purge_scope

    store = LocalJsonStore(tmp_path / "db.json")
    store.save_template({"id": "t1", "user": "guest", "name": "x", "sections": []})
    store.save_feedback({"id": "f1", "user": "guest", "note": "x"})
    store.save_job({"id": "j1", "user": "guest", "transcript": "x"})
    store.save_push_subscription({"id": "p1", "user": "guest", "endpoint": "https://x/1"})

    purge_scope(store, "guest")

    assert store.list_templates("guest") == []
    assert store.list_feedback("guest") == []
    assert store.list_jobs("guest") == []
    assert store.list_push_subscriptions("guest") == []


def test_leaves_groups_and_declines_invites_but_keeps_group_data(tmp_path):
    client = make_client(tmp_path, [])
    joined = make_group(client, "bob", "已加入")
    client.post(f"/api/groups/{joined}/invites", json={"email": "amy@gmail.com", "role": "editor"}, headers=h("bob"))
    client.post(f"/api/groups/{joined}/accept", headers=h("amy"))
    invited = make_group(client, "bob", "只有邀請")
    client.post(f"/api/groups/{invited}/invites", json={"email": "amy@gmail.com", "role": "viewer"}, headers=h("bob"))
    analyze(client, "amy", workspace=joined)  # amy 在群組裡建的會議屬於群組，留給其他人

    assert client.delete("/api/account", headers=h("amy")).status_code == 200

    assert client.store.groups_for("uid-amy", "amy@gmail.com") == []
    assert len(client.store.list_meetings(user=f"group:{joined}")) == 1


def test_group_owner_must_disband_first(tmp_path):
    deleted = []
    client = make_client(tmp_path, deleted)
    make_group(client, "amy", "我的群組")
    analyze(client, "amy")

    resp = client.delete("/api/account", headers=h("amy"))

    assert resp.status_code == 409
    assert "我的群組" in resp.json()["detail"]
    assert len(client.store.list_meetings(user="uid-amy")) == 1  # 什麼都沒刪
    assert deleted == []


def test_not_available_without_login(tmp_path):
    """共用 API Token 模式所有人是同一個使用者，刪下去等於清空整個服務。"""
    from tests.test_api import _limited_client

    assert _limited_client(tmp_path, "").delete("/api/account").status_code == 400


def test_firebase_failure_after_data_is_gone_still_succeeds(tmp_path):
    """資料已經刪光才去刪 Firebase 帳號；那一步失敗不能回 500 讓人以為什麼都沒刪。"""

    def boom(uid):
        raise RuntimeError("firebase down")

    settings = Settings(
        gemini_api_key=None, data_dir=tmp_path / "b",
        firebase_web_api_key="web-key", firebase_auth_domain="demo.firebaseapp.com", firebase_project_id="demo",
    )
    store = LocalJsonStore(tmp_path / "b.json")
    client = TestClient(create_app(settings, store=store, verify_token=fake_verify, delete_auth_user=boom))

    resp = client.delete("/api/account", headers=h("amy"))

    assert resp.status_code == 200
    assert resp.json()["auth_deleted"] is False
