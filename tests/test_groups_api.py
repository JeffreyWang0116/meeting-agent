"""群組工作區：建立、用信箱邀請、接受、角色權限、切換工作區看到群組資料。

假登入：token「tok-amy」→ uid「uid-amy」、已驗證信箱 amy@gmail.com。
群組資料蓋 group:<id>，前端每個請求帶 X-Workspace，中介層驗成員身分後切換範圍。
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
    # 「tok-nomail」模擬沒有已驗證信箱的帳號
    return {"uid": f"uid-{name}", "email": None if name == "nomail" else f"{name}@gmail.com"}


def make_client(tmp_path, *, auth=True, **extra):
    settings = Settings(
        gemini_api_key=None,
        data_dir=tmp_path,
        firebase_web_api_key="web-key" if auth else None,
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
    client = TestClient(create_app(settings, store=store, orchestrator=orchestrator, verify_token=fake_verify))
    client.store = store  # 驗「資料真的刪掉」要直接看 store
    return client


@pytest.fixture
def client(tmp_path):
    return make_client(tmp_path)


def h(name, workspace=None):
    headers = {"Authorization": f"Bearer tok-{name}"}
    if workspace:
        headers["X-Workspace"] = workspace
    return headers


def create_group(client, name="專題小組", who="amy"):
    resp = client.post("/api/groups", json={"name": name}, headers=h(who))
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def join(client, gid, who, role="editor"):
    assert client.post(f"/api/groups/{gid}/invites", json={"email": f"{who}@gmail.com", "role": role},
                       headers=h("amy")).status_code == 200
    assert client.post(f"/api/groups/{gid}/accept", headers=h(who)).status_code == 200


def add_meeting(client, who, workspace=None):
    resp = client.post("/api/meetings", json={"text": "志明下週一交 prompt"}, headers=h(who, workspace))
    assert resp.status_code == 200, resp.text
    return resp.json()["meeting_id"]


# ---- 建立、邀請、接受 ----

def test_groups_need_google_login(tmp_path):
    client = make_client(tmp_path, auth=False)
    assert client.get("/api/groups").status_code == 404


def test_create_group_makes_me_owner(client):
    gid = create_group(client)
    body = client.get("/api/groups", headers=h("amy")).json()
    assert body["groups"] == [{"id": gid, "name": "專題小組", "role": "owner",
                               "members": [{"uid": "uid-amy", "email": "amy@gmail.com", "role": "owner"}],
                               "invites": []}]
    assert body["invites"] == []


def test_group_name_is_required(client):
    assert client.post("/api/groups", json={"name": "  "}, headers=h("amy")).status_code == 400


def test_invitee_sees_invite_after_logging_in_with_that_email(client):
    gid = create_group(client)
    resp = client.post(f"/api/groups/{gid}/invites", json={"email": " Bob@Gmail.com ", "role": "viewer"},
                       headers=h("amy"))
    assert resp.status_code == 200
    invites = client.get("/api/groups", headers=h("bob")).json()["invites"]
    assert invites == [{"id": gid, "name": "專題小組", "role": "viewer", "invited_by": "amy@gmail.com"}]
    assert client.get("/api/groups", headers=h("cara")).json()["invites"] == []


def test_accept_turns_invite_into_membership(client):
    gid = create_group(client)
    join(client, gid, "bob", role="viewer")
    bob = client.get("/api/groups", headers=h("bob")).json()
    assert bob["invites"] == []
    assert [(g["id"], g["role"]) for g in bob["groups"]] == [(gid, "viewer")]


def test_only_the_invited_email_can_accept(client):
    gid = create_group(client)
    client.post(f"/api/groups/{gid}/invites", json={"email": "bob@gmail.com", "role": "editor"}, headers=h("amy"))
    assert client.post(f"/api/groups/{gid}/accept", headers=h("cara")).status_code == 403
    assert client.post(f"/api/groups/{gid}/accept", headers=h("nomail")).status_code == 403


def test_decline_removes_the_invite(client):
    gid = create_group(client)
    client.post(f"/api/groups/{gid}/invites", json={"email": "bob@gmail.com", "role": "editor"}, headers=h("amy"))
    assert client.post(f"/api/groups/{gid}/decline", headers=h("bob")).status_code == 200
    assert client.get("/api/groups", headers=h("bob")).json()["invites"] == []


def test_invite_validation(client):
    gid = create_group(client)
    bad = [{"email": "not-an-email", "role": "editor"}, {"email": "bob@gmail.com", "role": "owner"},
           {"email": "amy@gmail.com", "role": "editor"}]  # 最後一個已經是成員
    for body in bad:
        assert client.post(f"/api/groups/{gid}/invites", json=body, headers=h("amy")).status_code == 400, body


def test_reinviting_updates_the_role_instead_of_duplicating(client):
    gid = create_group(client)
    for role in ("viewer", "editor"):
        client.post(f"/api/groups/{gid}/invites", json={"email": "bob@gmail.com", "role": role}, headers=h("amy"))
    invites = client.get("/api/groups", headers=h("amy")).json()["groups"][0]["invites"]
    assert [(i["email"], i["role"]) for i in invites] == [("bob@gmail.com", "editor")]


def test_owner_can_revoke_an_invite(client):
    gid = create_group(client)
    client.post(f"/api/groups/{gid}/invites", json={"email": "bob@gmail.com", "role": "editor"}, headers=h("amy"))
    assert client.delete(f"/api/groups/{gid}/invites/bob@gmail.com", headers=h("amy")).status_code == 200
    assert client.get("/api/groups", headers=h("bob")).json()["invites"] == []


def test_only_owner_manages_members(client):
    gid = create_group(client)
    join(client, gid, "bob")
    assert client.post(f"/api/groups/{gid}/invites", json={"email": "cara@gmail.com", "role": "editor"},
                       headers=h("bob")).status_code == 403
    assert client.patch(f"/api/groups/{gid}", json={"name": "改名"}, headers=h("bob")).status_code == 403
    assert client.delete(f"/api/groups/{gid}", headers=h("bob")).status_code == 403
    assert client.post("/api/groups/nope/invites", json={"email": "x@gmail.com", "role": "editor"},
                       headers=h("amy")).status_code == 404


def test_owner_changes_roles_renames_and_removes(client):
    gid = create_group(client)
    join(client, gid, "bob")
    assert client.patch(f"/api/groups/{gid}/members/uid-bob", json={"role": "viewer"}, headers=h("amy")).status_code == 200
    assert client.get("/api/groups", headers=h("bob")).json()["groups"][0]["role"] == "viewer"
    assert client.patch(f"/api/groups/{gid}", json={"name": "新名字"}, headers=h("amy")).status_code == 200
    assert client.delete(f"/api/groups/{gid}/members/uid-bob", headers=h("amy")).status_code == 200
    assert client.get("/api/groups", headers=h("bob")).json()["groups"] == []
    # 建立者的角色改不掉、也不能被移除
    assert client.patch(f"/api/groups/{gid}/members/uid-amy", json={"role": "viewer"}, headers=h("amy")).status_code == 400


def test_member_can_leave_but_owner_cannot(client):
    gid = create_group(client)
    join(client, gid, "bob")
    assert client.delete(f"/api/groups/{gid}/members/uid-bob", headers=h("bob")).status_code == 200
    assert client.delete(f"/api/groups/{gid}/members/uid-amy", headers=h("amy")).status_code == 400


# ---- 工作區：群組的資料 ----

def test_group_workspace_shares_meetings_between_members(client):
    gid = create_group(client)
    join(client, gid, "bob")
    mid = add_meeting(client, "amy", gid)

    in_group = client.get("/api/meetings", headers=h("bob", gid)).json()["meetings"]
    assert [m["id"] for m in in_group] == [mid]
    assert client.get("/api/meetings", headers=h("bob")).json()["meetings"] == []   # 個人工作區看不到
    assert client.get("/api/meetings", headers=h("amy")).json()["meetings"] == []
    assert len(client.get("/api/tasks", headers=h("bob", gid)).json()["tasks"]) == 1


def test_non_member_cannot_enter_the_workspace(client):
    gid = create_group(client)
    assert client.get("/api/meetings", headers=h("cara", gid)).status_code == 403
    assert client.get("/api/meetings", headers=h("cara", "no-such-group")).status_code == 403
    # 被邀請但還沒接受也不行
    client.post(f"/api/groups/{gid}/invites", json={"email": "cara@gmail.com", "role": "editor"}, headers=h("amy"))
    assert client.get("/api/meetings", headers=h("cara", gid)).status_code == 403


def test_viewer_can_read_but_not_write(client):
    gid = create_group(client)
    join(client, gid, "bob", role="viewer")
    mid = add_meeting(client, "amy", gid)
    assert client.get(f"/api/meetings/{mid}", headers=h("bob", gid)).status_code == 200
    assert client.get("/api/search?q=prompt", headers=h("bob", gid)).status_code == 200
    denied = [
        ("post", "/api/meetings", {"text": "x"}),
        ("patch", f"/api/meetings/{mid}", {"title": "x"}),
        ("delete", f"/api/meetings/{mid}", None),
        ("post", "/api/tasks", {"task": "x"}),
        ("put", "/api/glossary", {"terms": []}),
    ]
    for method, url, body in denied:
        kwargs = {"headers": h("bob", gid)}
        if body is not None:
            kwargs["json"] = body
        assert getattr(client, method)(url, **kwargs).status_code == 403, url
    # 問答不改資料：viewer 可以用（這裡沒有金鑰，不是 403 就代表有放行）
    assert client.post("/api/ask", json={"question": "誰負責？"}, headers=h("bob", gid)).status_code != 403


def test_editor_can_write_in_group(client):
    gid = create_group(client)
    join(client, gid, "bob", role="editor")
    mid = add_meeting(client, "bob", gid)
    assert client.patch(f"/api/meetings/{mid}", json={"title": "改過"}, headers=h("amy", gid)).status_code == 200


def test_group_glossary_is_shared_and_separate_from_personal(client):
    gid = create_group(client)
    join(client, gid, "bob")
    client.put("/api/glossary", json={"terms": [{"term": "王小明", "note": ""}]}, headers=h("amy", gid))
    assert [t["term"] for t in client.get("/api/glossary", headers=h("bob", gid)).json()["terms"]] == ["王小明"]
    assert client.get("/api/glossary", headers=h("amy")).json()["terms"] == []


def test_backup_and_restore_are_personal_only(client):
    gid = create_group(client)
    assert client.get("/api/backup", headers=h("amy", gid)).status_code == 400
    assert client.post("/api/restore", json={"meetings": [], "tasks": []}, headers=h("amy", gid)).status_code == 400


def test_disband_deletes_group_data_and_access(client):
    gid = create_group(client)
    join(client, gid, "bob")
    add_meeting(client, "amy", gid)
    client.post("/api/tasks", json={"task": "手動任務"}, headers=h("amy", gid))
    client.put("/api/glossary", json={"terms": [{"term": "群組詞", "note": ""}]}, headers=h("amy", gid))
    assert client.delete(f"/api/groups/{gid}", headers=h("amy")).status_code == 200
    assert client.get("/api/groups", headers=h("bob")).json()["groups"] == []
    assert client.get("/api/meetings", headers=h("amy", gid)).status_code == 403
    # 資料要真的刪掉：之後就算有人拿到同一個 id 也撈不回來
    store_dump = (client.store.list_meetings(user=f"group:{gid}"),
                  client.store.list_tasks(user=f"group:{gid}"),
                  client.store.get_glossary(user=f"group:{gid}"))
    assert store_dump == ([], [], [])


def test_rate_limit_counts_people_not_workspaces(tmp_path):
    client = make_client(tmp_path, rate_limit_enabled=True, rate_limits="analyze=1/100")
    gid = create_group(client)
    join(client, gid, "bob")
    add_meeting(client, "amy", gid)
    # 同一個人換到個人工作區也不能多拿一份額度
    assert client.post("/api/meetings", json={"text": "x"}, headers=h("amy")).status_code == 429
    # 同群組的另一個人有自己的額度
    add_meeting(client, "bob", gid)


def test_summary_says_who_i_am(client):
    """前端要知道自己的 uid 才能「退出群組」、寄邀請信時寫上邀請人。"""
    assert client.get("/api/groups", headers=h("amy")).json()["me"] == {"uid": "uid-amy", "email": "amy@gmail.com"}
