"""留言的端點。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.decision_agent import DecisionAgent
from app.config import Settings
from app.main import create_app
from app.orchestrator import Orchestrator
from app.stores.local_store import LocalJsonStore
from tests.test_decision import valid_json


def h(uid="amy", workspace="g1"):
    out = {"Authorization": f"Bearer {uid}"}
    if workspace:
        out["X-Workspace"] = workspace
    return out


@pytest.fixture
def client(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    store.save_group({
        "id": "g1", "name": "專題組", "owner": "uid-amy",
        "members": [{"uid": "uid-amy", "email": "amy@example.com", "role": "owner"},
                    {"uid": "uid-bob", "email": "bob@example.com", "role": "editor"},
                    {"uid": "uid-cat", "email": "cat@example.com", "role": "viewer"}],
        "member_uids": ["uid-amy", "uid-bob", "uid-cat"], "invites": [], "invite_emails": [],
    })
    orchestrator = Orchestrator(decision=DecisionAgent(generate=lambda p: valid_json()), store=store)
    app = create_app(
        Settings(gemini_api_key=None, data_dir=tmp_path,
                 firebase_web_api_key="k", firebase_auth_domain="d"),
        store=store, orchestrator=orchestrator,
        verify_token=lambda token: {"uid": f"uid-{token}", "email": f"{token}@example.com"},
    )
    c = TestClient(app)
    c.store = store
    return c


def meeting(client):
    return client.post("/api/meetings", json={"text": "志明下週一交 prompt"},
                       headers=h()).json()["meeting_id"]


def post(client, mid, target="summary", text="這條其實還沒定案", uid="amy"):
    return client.post(f"/api/meetings/{mid}/comments",
                       json={"target": target, "text": text}, headers=h(uid))


def test_a_comment_can_be_left_and_read_back(client):
    mid = meeting(client)

    assert post(client, mid).status_code == 200

    body = client.get(f"/api/meetings/{mid}/comments", headers=h()).json()
    assert len(body["comments"]) == 1
    assert body["comments"][0]["text"] == "這條其實還沒定案"
    assert body["comments"][0]["author_name"] == "amy"


def test_comments_are_grouped_by_where_they_hang(client):
    """前端要一次拿齊才畫得出「摘要旁 2 則、第 3 條決議旁 1 則」。"""
    mid = meeting(client)
    post(client, mid, "summary")
    post(client, mid, "decision:0")
    post(client, mid, "decision:0")

    body = client.get(f"/api/meetings/{mid}/comments", headers=h()).json()

    assert body["counts"] == {"summary": 1, "decision:0": 2}


def test_everyone_in_the_group_sees_the_same_thread(client):
    mid = meeting(client)
    post(client, mid, uid="amy")
    post(client, mid, text="我同意", uid="bob")

    body = client.get(f"/api/meetings/{mid}/comments", headers=h("bob")).json()
    assert [c["author_name"] for c in body["comments"]] == ["amy", "bob"]


def test_a_view_only_member_can_still_comment(client):
    """「只能看」是不能改資料，不是不能說話——討論本來就是看的人最需要的。"""
    mid = meeting(client)

    assert post(client, mid, uid="cat").status_code == 200


def test_a_bad_target_is_rejected(client):
    mid = meeting(client)

    assert post(client, mid, target="whatever:1").status_code == 400


def test_an_empty_comment_is_rejected(client):
    mid = meeting(client)

    assert post(client, mid, text="   ").status_code == 400


def test_commenting_on_a_meeting_that_is_not_there(client):
    assert post(client, "nope").status_code == 404


def test_you_can_delete_your_own(client):
    mid = meeting(client)
    cid = post(client, mid).json()["comment"]["id"]

    assert client.delete(f"/api/comments/{cid}", headers=h()).status_code == 200
    assert client.get(f"/api/meetings/{mid}/comments", headers=h()).json()["comments"] == []


def test_you_cannot_delete_someone_elses(client):
    """群組裡可編輯的人很多，但別人說過的話不該被默默抹掉。"""
    mid = meeting(client)
    cid = post(client, mid, uid="amy").json()["comment"]["id"]

    assert client.delete(f"/api/comments/{cid}", headers=h("bob")).status_code == 403
    assert len(client.get(f"/api/meetings/{mid}/comments", headers=h()).json()["comments"]) == 1


def test_commenting_shows_up_in_the_activity_log(client):
    mid = meeting(client)
    post(client, mid)

    assert client.get("/api/activity", headers=h()).json()["activity"][0]["action"] == "comment.add"


def test_permanently_deleting_a_meeting_takes_its_comments_with_it(client):
    mid = meeting(client)
    post(client, mid)
    client.delete(f"/api/meetings/{mid}", headers=h())
    client.delete(f"/api/trash/{mid}", headers=h())

    assert client.store.list_comments("group:g1") == []


def test_trashing_a_meeting_keeps_its_comments_for_the_restore(client):
    """還沒永久刪除就把討論丟掉的話，救回來的是一場沒有討論的會議。"""
    mid = meeting(client)
    post(client, mid)
    client.delete(f"/api/meetings/{mid}", headers=h())

    assert len(client.store.list_comments("group:g1")) == 1

    client.post(f"/api/trash/{mid}/restore", headers=h())
    assert len(client.get(f"/api/meetings/{mid}/comments", headers=h()).json()["comments"]) == 1
