"""跨會議追蹤的端點。"""
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


def reply(title, decisions, pending, todos):
    return json.dumps({
        "meeting": {"title": title, "date": "2026-10-26", "summary": "摘要",
                    "attendees": ["林佳蓉", "陳柏宇"]},
        "decisions": [{"description": d} for d in decisions],
        "pending_items": [{"topic": t, "reason": None} for t in pending],
        "todos": todos, "highlights": [], "sections": [], "tags": [],
    }, ensure_ascii=False)


@pytest.fixture
def client(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    script = {"next": reply("會議", [], [], [])}
    orchestrator = Orchestrator(
        decision=DecisionAgent(generate=lambda p: script["next"]), store=store)
    app = create_app(Settings(gemini_api_key=None, data_dir=tmp_path),
                     store=store, orchestrator=orchestrator, clock=lambda: NOW)
    c = TestClient(app)
    c.store = store
    c.script = script
    return c


def analyze(client, title, decisions=(), pending=(), todos=()):
    client.script["next"] = reply(title, list(decisions), list(pending), list(todos))
    return client.post("/api/meetings", json={"text": "逐字稿"}).json()["meeting_id"]


def test_open_items_groups_across_meetings(client):
    analyze(client, "第一週", pending=["講者邀請"])
    analyze(client, "第二週", pending=["講者邀請"])

    items = client.get("/api/insights/open-items").json()["items"]

    assert len(items) == 1
    assert items[0]["times"] == 2


def test_open_items_is_empty_before_there_is_anything(client):
    assert client.get("/api/insights/open-items").json()["items"] == []


def test_people_counts_attendance_and_tasks(client):
    analyze(client, "第一週",
            todos=[{"task": "整理預算表", "owner": "林佳蓉", "priority": "high"}])

    people = {p["name"]: p for p in client.get("/api/insights/people").json()["people"]}

    assert people["林佳蓉"]["meetings"] == 1
    assert people["林佳蓉"]["tasks"] == 1
    assert people["陳柏宇"]["tasks"] == 0


def test_a_trashed_meeting_drops_out_of_the_numbers(client):
    """刪掉的會議還算在出席場次裡，那份統計就沒有人會信。"""
    mid = analyze(client, "第一週", pending=["講者邀請"])
    client.delete(f"/api/meetings/{mid}")

    assert client.get("/api/insights/open-items").json()["items"] == []
    assert client.get("/api/insights/people").json()["people"] == []


def test_the_weekly_report_covers_the_last_seven_days(client):
    analyze(client, "本週", decisions=["預算維持原案"])

    report = client.get("/api/insights/report?days=7").json()

    assert report["meetings"] == 1
    assert report["decisions"] == 1
    assert report["start"] == "2026-10-20"
    assert report["end"] == "2026-10-26"


def test_the_monthly_report_is_a_wider_window(client):
    report = client.get("/api/insights/report?days=30").json()

    assert report["start"] == "2026-09-27"


def test_other_windows_are_refused(client):
    assert client.get("/api/insights/report?days=3").status_code == 400
