"""Firebase Auth（Google 登入）：ID token → 使用者 id。

設了前端 Firebase 設定就啟用真正的帳號制：每個請求帶 Google 登入拿到的
ID token，後端驗簽後取 uid 當使用者，資料照 uid 分開。沒設定就維持原本的
單人模式（API_TOKEN 是一把共用鑰匙，所有人都是 DEFAULT_USER），本機開發
不必為了跑起來去申請 Firebase 專案。

這裡注入假的驗簽函式，不觸網、也不需要真的 Firebase 專案。
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

# 假的 ID token 驗簽：只認得 "tok-<名字>" 這種格式，其餘一律無效——真正的
# verify_id_token 也是這樣挑剔（不是 Firebase 簽的就不通過），假件太寬鬆會讓
# 「共用 token 不能繞過帳號制」那條測試假性通過
def fake_verify(id_token: str) -> str:
    if not id_token.startswith("tok-") or not id_token[4:]:
        raise AuthError("登入憑證無效或已過期")
    return f"uid-{id_token[4:]}"


def make_app(tmp_path, *, auth=True, verify=fake_verify, **extra):
    settings = Settings(
        gemini_api_key=None,
        data_dir=tmp_path,
        firebase_web_api_key="web-key-123" if auth else None,
        firebase_auth_domain="demo.firebaseapp.com" if auth else None,
        firebase_project_id="demo" if auth else None,
        **extra,
    )
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(generate=lambda p: valid_json()),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(tmp_path / "notifications"),
    )
    app = create_app(
        settings, store=store, orchestrator=orchestrator, verify_token=verify
    )
    return TestClient(app), store


def as_user(client, name):
    """帶著某個人的 ID token 發請求。"""
    return {"Authorization": f"Bearer tok-{name}"}


# ---- 開關 ----

def test_auth_disabled_without_firebase_web_config(tmp_path):
    client, _ = make_app(tmp_path, auth=False)
    assert client.get("/api/auth/config").json()["enabled"] is False
    assert client.get("/api/meetings").status_code == 200  # 單人模式照舊


def test_auth_config_gives_the_frontend_what_it_needs_to_sign_in(tmp_path):
    client, _ = make_app(tmp_path)
    body = client.get("/api/auth/config").json()
    assert body == {
        "enabled": True,
        "apiKey": "web-key-123",
        "authDomain": "demo.firebaseapp.com",
        "projectId": "demo",
    }


# ---- 認證 ----

def test_request_without_token_rejected(tmp_path):
    client, _ = make_app(tmp_path)
    assert client.get("/api/meetings").status_code == 401


def test_invalid_token_rejected(tmp_path):
    client, _ = make_app(tmp_path)
    # HTTP 標頭只能放 latin-1，真正的 ID token 是 ASCII 的 JWT
    resp = client.get("/api/meetings", headers={"Authorization": "Bearer not-a-firebase-jwt"})
    assert resp.status_code == 401


def test_valid_token_allows_the_request(tmp_path):
    client, _ = make_app(tmp_path)
    assert client.get("/api/meetings", headers=as_user(None, "alice")).status_code == 200


def test_public_endpoints_need_no_login(tmp_path):
    client, _ = make_app(tmp_path)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/auth/config").status_code == 200
    assert client.get("/").status_code == 200  # 登入畫面本身要載得進來


# ---- 這一切的重點：資料真的分開 ----

def test_each_account_gets_its_own_meetings(tmp_path):
    client, store = make_app(tmp_path)

    client.post("/api/meetings", json={"text": "Alice 的會議"}, headers=as_user(None, "alice"))
    client.post("/api/meetings", json={"text": "Bob 的會議"}, headers=as_user(None, "bob"))

    alice = client.get("/api/meetings", headers=as_user(None, "alice")).json()["meetings"]
    bob = client.get("/api/meetings", headers=as_user(None, "bob")).json()["meetings"]

    assert len(alice) == 1 and len(bob) == 1
    assert alice[0]["id"] != bob[0]["id"]
    # 資料庫裡蓋的是 Firebase uid，不是共用的 DEFAULT_USER
    assert {m["user"] for m in store.list_meetings(user="uid-alice")} == {"uid-alice"}


def test_one_account_cannot_open_anothers_meeting(tmp_path):
    client, _ = make_app(tmp_path)
    meeting_id = client.post(
        "/api/meetings", json={"text": "Alice 的會議"}, headers=as_user(None, "alice")
    ).json()["meeting_id"]

    assert client.get(f"/api/meetings/{meeting_id}", headers=as_user(None, "alice")).status_code == 200
    assert client.get(f"/api/meetings/{meeting_id}", headers=as_user(None, "bob")).status_code == 404


def test_glossary_and_speakers_are_per_account(tmp_path):
    client, _ = make_app(tmp_path)
    client.put(
        "/api/glossary",
        json={"terms": [{"term": "王霖翔", "note": "人名"}]},
        headers=as_user(None, "alice"),
    )
    client.put("/api/speakers", json={"names": ["Alice"]}, headers=as_user(None, "alice"))

    assert client.get("/api/glossary", headers=as_user(None, "bob")).json()["terms"] == []
    assert client.get("/api/speakers", headers=as_user(None, "bob")).json()["names"] == []
    assert client.get("/api/glossary", headers=as_user(None, "alice")).json()["terms"]


def test_tasks_are_per_account(tmp_path):
    client, _ = make_app(tmp_path)
    client.post("/api/tasks", json={"task": "Alice 的待辦"}, headers=as_user(None, "alice"))

    assert client.get("/api/tasks", headers=as_user(None, "bob")).json()["tasks"] == []
    assert len(client.get("/api/tasks", headers=as_user(None, "alice")).json()["tasks"]) == 1


def test_backup_only_contains_your_own_data(tmp_path):
    client, _ = make_app(tmp_path)
    client.post("/api/meetings", json={"text": "Alice 的會議"}, headers=as_user(None, "alice"))
    client.post("/api/meetings", json={"text": "Bob 的會議"}, headers=as_user(None, "bob"))

    backup = client.get("/api/backup", headers=as_user(None, "bob")).json()
    assert len(backup["meetings"]) == 1


# ---- 兩種模式不會互相干擾 ----

def test_shared_api_token_still_works_when_firebase_not_configured(tmp_path):
    client, _ = make_app(tmp_path, auth=False, api_token="secret123")
    assert client.get("/api/meetings").status_code == 401
    assert client.get(
        "/api/meetings", headers={"Authorization": "Bearer secret123"}
    ).status_code == 200


def test_firebase_login_takes_precedence_over_shared_token(tmp_path):
    """兩個都設的時候以 Firebase 為準：共用鑰匙不該還能繞過帳號制。"""
    client, _ = make_app(tmp_path, api_token="secret123")
    assert client.get(
        "/api/meetings", headers={"Authorization": "Bearer secret123"}
    ).status_code == 401
    assert client.get("/api/meetings", headers=as_user(None, "alice")).status_code == 200


# ---- 輔助函式 ----

def test_bearer_token_parsing():
    from app.auth import bearer_token

    assert bearer_token("Bearer abc") == "abc"
    assert bearer_token("bearer abc") == "abc"  # 大小寫不該影響
    assert bearer_token("abc") is None  # 少了 scheme
    assert bearer_token("Bearer   ") is None
    assert bearer_token(None) is None


# ---- 半套設定 ----

def test_login_without_admin_credentials_fails_loudly(tmp_path):
    """只設了前端那組、沒給 service account 金鑰＝半套：驗 ID token 需要金鑰。

    這時候絕對不能悄悄退回單人模式——那會讓人以為網站已經上鎖，實際上是全開
    的，而且完全沒有徵兆。寧可啟動就失敗，訊息直接說少了哪個環境變數。
    """
    settings = Settings(
        gemini_api_key=None,
        data_dir=tmp_path,
        firebase_web_api_key="web-key-123",
        firebase_auth_domain="demo.firebaseapp.com",
    )
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(generate=lambda p: valid_json()),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(tmp_path / "notifications"),
    )

    with pytest.raises(RuntimeError, match="FIREBASE_CREDENTIALS"):
        create_app(settings, store=store, orchestrator=orchestrator)


# ---- 公開部署卻完全沒有認證 ----

def _plain_deps(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(generate=lambda p: valid_json()),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(tmp_path / "notifications"),
    )
    return {"store": store, "orchestrator": orchestrator}


def test_public_deploy_with_no_auth_at_all_refuses_to_start(tmp_path):
    """兩種把關方式都沒設，卻跑在公開網址上＝全世界都能讀寫刪除所有會議、
    燒光 Gemini 額度，而且完全沒有徵兆。

    這是最容易犯的錯：render.yaml 裡這些值都是部署後才在後台填的，忘了填服務
    照樣起得來、首頁照樣打得開，看起來一切正常。所以寧可啟動就失敗——理由與
    上面那條半套 Firebase 設定完全相同。
    """
    settings = Settings(gemini_api_key=None, data_dir=tmp_path, is_public_deploy=True)

    with pytest.raises(RuntimeError, match="API_TOKEN"):
        create_app(settings, **_plain_deps(tmp_path))


def test_shared_key_is_enough_to_start_a_public_deploy(tmp_path):
    """共用鑰匙不是帳號制，但確實把門關上了，不該被擋。"""
    settings = Settings(
        gemini_api_key=None,
        data_dir=tmp_path,
        is_public_deploy=True,
        api_token="shared-key",
    )
    assert create_app(settings, **_plain_deps(tmp_path)) is not None


def test_explicit_opt_in_allows_a_public_deploy_with_no_door(tmp_path):
    """真的想開一個沒有門的公開站是他家的事——但要說出口，不能用沉默表示。"""
    settings = Settings(
        gemini_api_key=None,
        data_dir=tmp_path,
        is_public_deploy=True,
        allow_no_auth=True,
    )
    assert create_app(settings, **_plain_deps(tmp_path)) is not None


def test_local_dev_without_auth_is_still_fine(tmp_path):
    """本機開發不設認證是刻意的方便，守衛不該波及。"""
    settings = Settings(gemini_api_key=None, data_dir=tmp_path)
    assert create_app(settings, **_plain_deps(tmp_path)) is not None


# ---- 驗簽失敗要攤出真正原因 ----

def test_verify_surfaces_the_real_firebase_reason(monkeypatch):
    """firebase 的例外訊息（aud 專案不符／時鐘偏移／過期）是唯一的線索，
    不能只留型別名。"""
    import firebase_admin.auth as fa

    from app.auth import AuthError, verify_firebase_id_token

    def boom(_token):
        raise ValueError(
            "The Firebase ID token has incorrect 'aud' (audience) claim. "
            "Expected 'proj-A' but got 'proj-B'."
        )

    monkeypatch.setattr(fa, "verify_id_token", boom)
    with pytest.raises(AuthError, match="aud.*Expected 'proj-A' but got 'proj-B'"):
        verify_firebase_id_token("some-token")


def test_mismatched_firebase_projects_refuse_to_start(tmp_path):
    """四個值混到兩個專案時，啟動就該失敗並指名是哪兩個對不上。

    這是 2026-09-08 實際發生的事：兩個人各自把自己 Firebase 專案的金鑰填進
    同一個 Render 服務。站台看起來完全正常（health 200、首頁打得開、登入畫面
    過得去），但每個 API 都失敗，而錯誤訊息把人往轉錄和額度的方向帶，查了
    一整晚。這道守衛會讓它在部署當下就爆。
    """
    settings = Settings(
        gemini_api_key=None,
        data_dir=tmp_path,
        firebase_web_api_key="web-key",
        firebase_auth_domain="meeting-agent-aaaaa.firebaseapp.com",
        firebase_project_id="meeting-agent-aaaaa",
        firebase_credentials_json='{"type":"service_account","project_id":"meeting-agent-bbbbb"}',
    )

    with pytest.raises(RuntimeError, match="meeting-agent-bbbbb"):
        create_app(settings, **_plain_deps(tmp_path))
