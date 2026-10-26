"""垃圾桶的端點：刪除改成先丟進去，30 天內救得回來。

群組裡任何一個「可編輯」的成員誤刪，影響的是所有人；在這之前完全沒有
復原的可能。30 天到了才真的清掉。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.agents.decision_agent import DecisionAgent
from app.config import Settings
from app.main import create_app
from app.orchestrator import Orchestrator
from app.stores.local_store import LocalJsonStore
from tests.test_decision import valid_json

NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


def make(tmp_path, now=None):
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(decision=DecisionAgent(generate=lambda p: valid_json()), store=store)
    app = create_app(
        Settings(gemini_api_key=None, data_dir=tmp_path),
        store=store, orchestrator=orchestrator, clock=(lambda: now) if now else None,
    )
    client = TestClient(app)
    client.store = store
    return client


def meeting(client):
    return client.post("/api/meetings", json={"text": "志明下週一交 prompt"}).json()["meeting_id"]


def test_deleting_moves_it_to_the_trash_instead_of_destroying_it(tmp_path):
    client = make(tmp_path, NOW)
    mid = meeting(client)

    assert client.delete(f"/api/meetings/{mid}").status_code == 200

    assert client.get("/api/meetings").json()["meetings"] == []
    trash = client.get("/api/trash").json()["trash"]
    assert [m["id"] for m in trash] == [mid]
    assert trash[0]["deleted_at"].startswith("2026-10-26")


def test_the_trash_says_how_long_is_left(tmp_path):
    """「30 天內可以救回來」要看得到剩幾天，不然等於沒有期限這回事。"""
    client = make(tmp_path, NOW)
    mid = meeting(client)
    client.delete(f"/api/meetings/{mid}")

    assert client.get("/api/trash").json()["trash"][0]["days_left"] == 30


def test_restoring_brings_it_back(tmp_path):
    client = make(tmp_path, NOW)
    mid = meeting(client)
    client.delete(f"/api/meetings/{mid}")

    assert client.post(f"/api/trash/{mid}/restore").status_code == 200

    assert [m["id"] for m in client.get("/api/meetings").json()["meetings"]] == [mid]
    assert client.get("/api/trash").json()["trash"] == []
    assert len(client.get("/api/tasks").json()["tasks"]) >= 1


def test_restoring_something_that_is_not_there_is_404(tmp_path):
    assert make(tmp_path).post("/api/trash/nope/restore").status_code == 404


def test_emptying_one_item_for_good(tmp_path):
    client = make(tmp_path, NOW)
    mid = meeting(client)
    client.delete(f"/api/meetings/{mid}")

    assert client.delete(f"/api/trash/{mid}").status_code == 200

    assert client.get("/api/trash").json()["trash"] == []
    assert client.post(f"/api/trash/{mid}/restore").status_code == 404


def test_things_older_than_thirty_days_are_cleared_out(tmp_path):
    """排程已經有一支了（推播），不值得再加一支。開垃圾桶的時候順手清。"""
    client = make(tmp_path, NOW)
    mid = meeting(client)
    client.delete(f"/api/meetings/{mid}")

    later = make(tmp_path, NOW + timedelta(days=31))
    body = later.get("/api/trash").json()

    assert body["trash"] == []
    assert later.post(f"/api/trash/{mid}/restore").status_code == 404


def test_something_deleted_twenty_nine_days_ago_is_still_there(tmp_path):
    client = make(tmp_path, NOW)
    mid = meeting(client)
    client.delete(f"/api/meetings/{mid}")

    later = make(tmp_path, NOW + timedelta(days=29))
    trash = later.get("/api/trash").json()["trash"]

    assert [m["id"] for m in trash] == [mid]
    assert trash[0]["days_left"] == 1


def test_a_trashed_meeting_cannot_be_read_or_edited(tmp_path):
    """救回來之前它不該出現在任何別的地方，否則「刪掉了」就是騙人的。"""
    client = make(tmp_path, NOW)
    mid = meeting(client)
    client.delete(f"/api/meetings/{mid}")

    assert client.get(f"/api/meetings/{mid}").status_code == 404
    assert client.patch(f"/api/meetings/{mid}", json={"title": "x"}).status_code == 404


def test_deleting_the_same_meeting_twice_is_404_not_a_second_trash_entry(tmp_path):
    client = make(tmp_path, NOW)
    mid = meeting(client)
    client.delete(f"/api/meetings/{mid}")

    assert client.delete(f"/api/meetings/{mid}").status_code == 404
    assert len(client.get("/api/trash").json()["trash"]) == 1
