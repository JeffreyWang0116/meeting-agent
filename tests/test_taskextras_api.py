"""任務的子項目、重複任務、負責人建議。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.agents.decision_agent import DecisionAgent
from app.config import Settings
from app.main import create_app
from app.orchestrator import Orchestrator
from app.stores.local_store import LocalJsonStore

NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
REPLY = json.dumps({
    "meeting": {"title": "週會", "date": "2026-10-26", "summary": "摘要", "attendees": ["林佳蓉"]},
    "todos": [{"task": "整理預算表", "owner": "林佳蓉", "due_date": "2026-10-30", "priority": "high"}],
    "decisions": [], "pending_items": [], "highlights": [], "sections": [], "tags": [],
}, ensure_ascii=False)


def make(tmp_path, *, group=False):
    store = LocalJsonStore(tmp_path / "db.json")
    if group:
        store.save_group({
            "id": "g1", "name": "專題組", "owner": "uid-amy",
            "members": [{"uid": "uid-amy", "email": "amy@example.com", "role": "owner"},
                        {"uid": "uid-bob", "email": "bob@example.com", "role": "editor"}],
            "member_uids": ["uid-amy", "uid-bob"], "invites": [], "invite_emails": [],
        })
    orchestrator = Orchestrator(decision=DecisionAgent(generate=lambda p: REPLY), store=store)
    settings = Settings(gemini_api_key=None, data_dir=tmp_path,
                        **({"firebase_web_api_key": "k", "firebase_auth_domain": "d"} if group else {}))
    app = create_app(settings, store=store, orchestrator=orchestrator, clock=lambda: NOW,
                     verify_token=(lambda t: {"uid": f"uid-{t}", "email": f"{t}@example.com"})
                     if group else None)
    c = TestClient(app)
    c.store = store
    return c


@pytest.fixture
def client(tmp_path):
    return make(tmp_path)


@pytest.fixture
def task(client):
    client.post("/api/meetings", json={"text": "逐字稿"})
    return client.get("/api/tasks").json()["tasks"][0]


# ---- 子任務 ----

def test_subtasks_can_be_added(client, task):
    resp = client.patch(f"/api/tasks/{task['id']}", json={"subtasks": ["列出項目", "對帳"]})

    assert resp.status_code == 200
    assert resp.json()["subtasks"] == [
        {"text": "列出項目", "done": False}, {"text": "對帳", "done": False}]


def test_a_subtask_can_be_ticked(client, task):
    client.patch(f"/api/tasks/{task['id']}", json={"subtasks": ["列出項目", "對帳"]})

    client.patch(f"/api/tasks/{task['id']}",
                 json={"subtasks": [{"text": "列出項目", "done": True}, {"text": "對帳"}]})

    saved = client.get("/api/tasks").json()["tasks"][0]["subtasks"]
    assert [s["done"] for s in saved] == [True, False]


def test_blank_subtasks_do_not_survive(client, task):
    client.patch(f"/api/tasks/{task['id']}", json={"subtasks": ["列出項目", "  "]})

    assert len(client.get("/api/tasks").json()["tasks"][0]["subtasks"]) == 1


def test_an_absurd_number_of_subtasks_is_refused(client, task):
    resp = client.patch(f"/api/tasks/{task['id']}", json={"subtasks": [f"{i}" for i in range(200)]})

    assert resp.status_code == 400


# ---- 重複任務 ----

def test_completing_a_repeating_task_schedules_the_next_one(client, task):
    client.patch(f"/api/tasks/{task['id']}", json={"recurrence": "weekly"})

    client.patch(f"/api/tasks/{task['id']}", json={"status": "done"})

    tasks = client.get("/api/tasks").json()["tasks"]
    assert len(tasks) == 2
    nxt = next(t for t in tasks if t["id"] != task["id"])
    assert nxt["due_date"] == "2026-11-06"      # 原本 10/30 + 7 天
    assert nxt["status"] == "todo"
    assert nxt["recurrence"] == "weekly"
    assert nxt["owner"] == "林佳蓉"


def test_the_finished_one_stays_finished(client, task):
    """自動排下一筆不該把這一筆變回未完成——那會讓人以為按了沒用。"""
    client.patch(f"/api/tasks/{task['id']}", json={"recurrence": "weekly"})
    client.patch(f"/api/tasks/{task['id']}", json={"status": "done"})

    done = next(t for t in client.get("/api/tasks").json()["tasks"] if t["id"] == task["id"])
    assert done["status"] == "done"


def test_the_next_one_starts_with_a_clean_checklist(client, task):
    """上一輪勾完的子項目帶過來的話，這一輪一開始就是全部完成。"""
    client.patch(f"/api/tasks/{task['id']}",
                 json={"recurrence": "weekly", "subtasks": [{"text": "對帳", "done": True}]})

    client.patch(f"/api/tasks/{task['id']}", json={"status": "done"})

    nxt = next(t for t in client.get("/api/tasks").json()["tasks"] if t["id"] != task["id"])
    assert nxt["subtasks"] == [{"text": "對帳", "done": False}]


def test_completing_it_twice_does_not_schedule_two(client, task):
    client.patch(f"/api/tasks/{task['id']}", json={"recurrence": "weekly"})
    client.patch(f"/api/tasks/{task['id']}", json={"status": "done"})
    client.patch(f"/api/tasks/{task['id']}", json={"status": "done"})

    assert len(client.get("/api/tasks").json()["tasks"]) == 2


def test_a_one_off_task_does_not_repeat(client, task):
    client.patch(f"/api/tasks/{task['id']}", json={"status": "done"})

    assert len(client.get("/api/tasks").json()["tasks"]) == 1


def test_a_repeating_task_without_a_due_date_does_not_repeat(client, task):
    """沒有期限的重複任務等於無限產生一堆沒有期限的任務。"""
    client.patch(f"/api/tasks/{task['id']}", json={"recurrence": "weekly", "due_date": ""})

    client.patch(f"/api/tasks/{task['id']}", json={"status": "done"})

    assert len(client.get("/api/tasks").json()["tasks"]) == 1


def test_an_unknown_recurrence_is_refused(client, task):
    assert client.patch(f"/api/tasks/{task['id']}",
                        json={"recurrence": "每隔三天"}).status_code == 400


def test_recurrence_can_be_turned_off(client, task):
    client.patch(f"/api/tasks/{task['id']}", json={"recurrence": "weekly"})
    client.patch(f"/api/tasks/{task['id']}", json={"recurrence": None})

    client.patch(f"/api/tasks/{task['id']}", json={"status": "done"})
    assert len(client.get("/api/tasks").json()["tasks"]) == 1


# ---- 負責人建議 ----

def test_owners_already_used_are_suggested(client, task):
    names = client.get("/api/owners").json()["owners"]

    assert "林佳蓉" in names


def test_attendees_are_suggested_too(client, task):
    """指派給在場但還沒有任務的人，正是最常見的動作。"""
    assert "林佳蓉" in client.get("/api/owners").json()["owners"]


def test_group_members_are_suggested_in_a_group_workspace(tmp_path):
    client = make(tmp_path, group=True)
    h = {"Authorization": "Bearer amy", "X-Workspace": "g1"}

    body = client.get("/api/owners", headers=h).json()

    assert {"amy", "bob"} <= set(body["owners"])
    assert any(m["email"] == "bob@example.com" for m in body["members"])


def test_a_personal_workspace_has_no_members_to_assign_to(client):
    assert client.get("/api/owners").json()["members"] == []


def test_suggestions_have_no_duplicates(client, task):
    names = client.get("/api/owners").json()["owners"]

    assert len(names) == len(set(names))
