"""回報 AI 錯誤的 API，以及匯出成評估資料集。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.agents.decision_agent import DecisionAgent
from app.config import Settings
from app.main import create_app
from app.orchestrator import Orchestrator
from app.stores.local_store import LocalJsonStore
from tests.test_decision import valid_json


def make(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        decision=DecisionAgent(generate=lambda p: valid_json()),
        store=store,
    )
    client = TestClient(create_app(
        Settings(gemini_api_key=None, data_dir=tmp_path), store=store, orchestrator=orchestrator
    ))
    client.store = store
    return client


def make_meeting(client):
    return client.post("/api/meetings", json={"text": "志明下週一交 prompt"}).json()["meeting_id"]


def report(client, mid, **extra):
    body = {"meeting_id": mid, "target": "todo", "target_text": "完成 prompt 修改",
            "reason": "owner_wrong", **extra}
    return client.post("/api/feedback", json=body)


def test_reporting_and_listing(tmp_path):
    client = make(tmp_path)
    mid = make_meeting(client)

    assert report(client, mid, note="應該是小華").status_code == 200

    items = client.get("/api/feedback").json()["feedback"]
    assert len(items) == 1
    assert items[0]["note"] == "應該是小華"
    assert items[0]["reason"] == "owner_wrong"


def test_note_is_optional(tmp_path):
    client = make(tmp_path)
    mid = make_meeting(client)

    assert report(client, mid).status_code == 200
    assert client.get("/api/feedback").json()["feedback"][0]["note"] == ""


def test_unknown_reason_is_rejected(tmp_path):
    client = make(tmp_path)
    mid = make_meeting(client)

    assert report(client, mid, reason="隨便打的").status_code == 400


def test_reporting_on_someone_elses_meeting_is_404(tmp_path):
    client = make(tmp_path)

    assert report(client, "nope").status_code == 404


def test_deleting_a_report(tmp_path):
    client = make(tmp_path)
    mid = make_meeting(client)
    report(client, mid)
    fid = client.get("/api/feedback").json()["feedback"][0]["id"]

    assert client.delete(f"/api/feedback/{fid}").status_code == 200
    assert client.get("/api/feedback").json()["feedback"] == []


def test_export_is_jsonl_with_the_transcript_for_regression_testing(tmp_path):
    """匯出要能直接拿去跑 eval：沒有逐字稿就重現不了那次錯誤。"""
    client = make(tmp_path)
    mid = make_meeting(client)
    report(client, mid, note="應該是小華")

    resp = client.get("/api/feedback/export.jsonl")

    assert resp.status_code == 200
    lines = [line for line in resp.text.splitlines() if line.strip()]
    assert len(lines) == 1
    import json
    row = json.loads(lines[0])
    assert row["transcript"] == "志明下週一交 prompt"
    assert row["reported"]["reason"] == "owner_wrong"
    assert row["reported"]["note"] == "應該是小華"
