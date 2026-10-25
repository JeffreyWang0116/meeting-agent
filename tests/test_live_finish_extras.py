"""POST /api/live/{id}/finish 收下聆聽中按的標記與手打的筆記，交給分析。

兩者都是「會議當下」才有的資訊，結束後就補不回來，所以在 finish 一起送。
"""
from __future__ import annotations

import io

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class FakeTranscriber:
    def transcribe(self, path, on_progress=None, hint=None, user=None):
        return "[0:01] 講者A：大家好"


class RecordingOrchestrator:
    def __init__(self):
        self.calls = []

    def process_transcript(self, text, **kwargs):
        self.calls.append(kwargs)
        return {"meeting_id": "m1", "analysis": {}, "notifications": {}, "transcript": text}


def make(tmp_path):
    orch = RecordingOrchestrator()
    app = create_app(
        Settings(gemini_api_key=None, data_dir=tmp_path),
        orchestrator=orch,
        transcriber=FakeTranscriber(),
    )
    return TestClient(app), orch


def start_with_a_chunk(client):
    sid = client.post("/api/live/start").json()["session_id"]
    client.post(
        f"/api/live/{sid}/chunk",
        files={"file": ("c.webm", io.BytesIO(b"audio"), "audio/webm")},
    )
    return sid


def test_marks_and_notes_reach_the_analysis(tmp_path):
    client, orch = make(tmp_path)
    sid = start_with_a_chunk(client)

    resp = client.post(
        f"/api/live/{sid}/finish",
        json={"marks": ["5:23"], "notes": "記得確認口試時間"},
    )

    assert resp.status_code == 200
    assert orch.calls[-1]["marks"] == ["5:23"]
    assert orch.calls[-1]["notes"] == "記得確認口試時間"


def test_finishing_without_them_still_works(tmp_path):
    client, orch = make(tmp_path)
    sid = start_with_a_chunk(client)

    assert client.post(f"/api/live/{sid}/finish").status_code == 200
    assert orch.calls[-1]["marks"] is None
    assert orch.calls[-1]["notes"] is None


def test_absurdly_long_notes_are_rejected(tmp_path):
    """筆記直接進提示詞，不設上限等於讓人塞爆 token（和帳單）。"""
    client, orch = make(tmp_path)
    sid = start_with_a_chunk(client)

    resp = client.post(f"/api/live/{sid}/finish", json={"notes": "字" * 20001})

    assert resp.status_code == 422
