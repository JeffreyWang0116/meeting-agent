"""活動紀錄要真的被寫下來——漏掉任何一個動作，那個動作就查不出是誰做的。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.decision_agent import DecisionAgent
from app.config import Settings
from app.main import create_app
from app.orchestrator import Orchestrator
from app.stores.local_store import LocalJsonStore
from tests.test_decision import valid_json


def h(uid="amy"):
    return {"Authorization": f"Bearer {uid}", "X-Workspace": "g1"}


@pytest.fixture
def client(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    store.save_group({
        "id": "g1", "name": "專題組", "owner": "uid-amy",
        "members": [{"uid": "uid-amy", "email": "amy@example.com", "role": "owner"},
                    {"uid": "uid-bob", "email": "bob@example.com", "role": "editor"}],
        "member_uids": ["uid-amy", "uid-bob"], "invites": [], "invite_emails": [],
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


def actions(client):
    return [e["action"] for e in client.get("/api/activity", headers=h()).json()["activity"]]


def meeting(client, uid="amy"):
    return client.post("/api/meetings", json={"text": "志明下週一交 prompt"},
                       headers=h(uid)).json()["meeting_id"]


def test_creating_a_meeting_is_logged(client):
    meeting(client)

    assert actions(client) == ["meeting.create"]


def test_the_log_says_who_and_what(client):
    meeting(client, "bob")

    entry = client.get("/api/activity", headers=h()).json()["activity"][0]
    assert entry["actor_email"] == "bob@example.com"
    assert entry["target_label"]
    assert entry["text"].startswith("bob ")


def test_editing_is_logged(client):
    mid = meeting(client)
    client.patch(f"/api/meetings/{mid}", json={"title": "改過的標題"}, headers=h())

    assert actions(client)[0] == "meeting.edit"


def test_deleting_and_restoring_are_both_logged(client):
    mid = meeting(client)
    client.delete(f"/api/meetings/{mid}", headers=h())
    client.post(f"/api/trash/{mid}/restore", headers=h())

    assert actions(client)[:2] == ["meeting.restore", "meeting.trash"]


def test_permanently_deleting_is_logged(client):
    mid = meeting(client)
    client.delete(f"/api/meetings/{mid}", headers=h())
    client.delete(f"/api/trash/{mid}", headers=h())

    assert actions(client)[0] == "meeting.purge"


def test_task_changes_are_logged(client):
    meeting(client)
    task = client.get("/api/tasks", headers=h()).json()["tasks"][0]
    client.patch(f"/api/tasks/{task['id']}", json={"status": "done"}, headers=h())
    client.delete(f"/api/tasks/{task['id']}", headers=h())

    assert actions(client)[:2] == ["task.delete", "task.done"]


def test_reopening_a_task_is_told_apart_from_completing_it(client):
    meeting(client)
    task = client.get("/api/tasks", headers=h()).json()["tasks"][0]
    client.patch(f"/api/tasks/{task['id']}", json={"status": "done"}, headers=h())
    client.patch(f"/api/tasks/{task['id']}", json={"status": "todo"}, headers=h())

    assert actions(client)[0] == "task.reopen"


def test_a_manual_task_is_logged(client):
    client.post("/api/tasks", json={"task": "買便當"}, headers=h())

    assert actions(client) == ["task.add"]


def test_nothing_is_logged_in_a_personal_workspace(client):
    """自己一個人的工作區，每件事都是自己做的。"""
    client.post("/api/meetings", json={"text": "志明下週一交 prompt"},
                headers={"Authorization": "Bearer amy"})

    assert client.get("/api/activity", headers={"Authorization": "Bearer amy"}).json()["activity"] == []
