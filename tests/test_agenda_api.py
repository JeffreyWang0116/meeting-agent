"""會議前準備的端點：系列、上次沒做完的、議程對照結果的保存。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.agents.decision_agent import DecisionAgent
from app.config import Settings
from app.main import create_app
from app.orchestrator import Orchestrator
from app.stores.local_store import LocalJsonStore

REPLY = json.dumps({
    "meeting": {"title": "專題週會", "date": "2026-10-26", "summary": "摘要", "attendees": ["甲"]},
    "todos": [{"task": "整理預算表", "owner": "甲", "due_date": "2026-10-30", "priority": "high"}],
    "decisions": [], "highlights": [], "sections": [], "tags": [],
    "pending_items": [{"topic": "場地", "reason": "還沒問到"}],
    "agenda_review": [
        {"item": "預算審查", "status": "covered", "note": "維持原案"},
        {"item": "場地", "status": "missed", "note": None},
    ],
    "off_agenda": ["臨時提到的人力問題"],
}, ensure_ascii=False)


@pytest.fixture
def client(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    seen = {}

    def generate(prompt):
        seen["prompt"] = prompt
        return REPLY

    orchestrator = Orchestrator(decision=DecisionAgent(generate=generate), store=store)
    app = create_app(Settings(gemini_api_key=None, data_dir=tmp_path),
                     store=store, orchestrator=orchestrator)
    c = TestClient(app)
    c.store = store
    c.seen = seen
    return c


def analyze(client, **extra):
    return client.post("/api/meetings", json={"text": "[0:01] 甲：今天要談預算", **extra})


# ---- 議程 ----

def test_the_agenda_reaches_the_model(client):
    analyze(client, agenda="1. 預算審查\n2. 場地")

    assert "預算審查" in client.seen["prompt"]
    assert "agenda_review" in client.seen["prompt"]


def test_the_review_comes_back_with_the_result(client):
    body = analyze(client, agenda="預算審查\n場地").json()

    review = body["analysis"]["agenda_review"]
    assert [r["item"] for r in review] == ["預算審查", "場地"]
    assert body["analysis"]["off_agenda"] == ["臨時提到的人力問題"]


def test_the_review_is_saved_with_the_meeting(client):
    """重新打開歷史會議時還要看得到對照結果，不然只有分析完那一瞬間有用。"""
    mid = analyze(client, agenda="預算審查\n場地").json()["meeting_id"]

    record = client.get(f"/api/meetings/{mid}").json()
    assert record["agenda"] == ["預算審查", "場地"]
    assert len(record["agenda_review"]) == 2


def test_the_coverage_counts_come_with_it(client):
    body = analyze(client, agenda="預算審查\n場地").json()

    assert body["agenda_coverage"] == {"covered": 1, "partial": 0, "missed": 1}


def test_no_agenda_means_no_review(client):
    body = analyze(client).json()

    assert "agenda_review" not in client.seen["prompt"]
    assert body["analysis"]["agenda_review"] == []


# ---- 系列 ----

def test_series_names_are_listed_for_the_next_meeting(client):
    analyze(client, series="專題週會")
    analyze(client, series="幹部會議")

    names = client.get("/api/series").json()["series"]
    assert set(names) == {"專題週會", "幹部會議"}


def test_the_same_series_is_only_listed_once(client):
    analyze(client, series="專題週會")
    analyze(client, series="專題週會")

    assert client.get("/api/series").json()["series"] == ["專題週會"]


def test_carryover_brings_the_last_meetings_loose_ends(client):
    analyze(client, series="專題週會")

    body = client.get("/api/series/專題週會/carryover").json()

    assert body["from"]["title"] == "專題週會"
    assert any("整理預算表" in line for line in body["lines"])
    assert any("場地" in line for line in body["lines"])


def test_carryover_skips_what_is_already_done(client):
    analyze(client, series="專題週會")
    task = client.get("/api/tasks").json()["tasks"][0]
    client.patch(f"/api/tasks/{task['id']}", json={"status": "done"})

    lines = client.get("/api/series/專題週會/carryover").json()["lines"]

    assert not any("整理預算表" in line for line in lines)


def test_carryover_for_a_series_with_no_history_is_empty_not_an_error(client):
    """第一次開這個系列的會時，這支一定會被呼叫到。"""
    body = client.get("/api/series/還沒開過的會/carryover").json()

    assert body["lines"] == []
    assert body["from"] is None


def test_carryover_reaches_the_model_when_asked_for(client):
    analyze(client, series="專題週會")

    analyze(client, series="專題週會", carry_over=True)

    assert "上次會議留下來" in client.seen["prompt"]
    assert "整理預算表" in client.seen["prompt"]


def test_carryover_is_opt_in(client):
    analyze(client, series="專題週會")

    analyze(client, series="專題週會")

    assert "上次會議留下來" not in client.seen["prompt"]


def test_a_trashed_meeting_does_not_haunt_the_next_one(client):
    mid = analyze(client, series="專題週會").json()["meeting_id"]
    client.delete(f"/api/meetings/{mid}")

    assert client.get("/api/series/專題週會/carryover").json()["lines"] == []
    assert client.get("/api/series").json()["series"] == []
