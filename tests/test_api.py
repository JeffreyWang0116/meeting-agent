"""FastAPI 端點整合測試。

Gemini 以假 generate 取代、Whisper 以假 transcriber 取代，
其餘（store、live session、media job、pipeline）全部走真實程式碼。
"""
import io
import time

import pytest
from fastapi.testclient import TestClient

from app.agents.decision_agent import DecisionAgent
from app.agents.executor_agent import ExecutorAgent
from app.agents.notifier_agent import NotifierAgent
from app.agents.parser_agent import ParserAgent
from app.config import Settings
from app.main import create_app
from app.orchestrator import Orchestrator
from app.stores.local_store import LocalJsonStore
from tests.test_decision import valid_json


class FakeTranscriber:
    device = "cpu"
    model_size = "fake"

    def transcribe(self, path, on_progress=None):
        if on_progress:
            on_progress(1.0, "假逐字稿")
        return "Kevin 說週五要 demo，鈺翔負責 prompt。"


@pytest.fixture
def client(tmp_path):
    settings = Settings(gemini_api_key=None, data_dir=tmp_path)
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(generate=lambda prompt: valid_json()),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(tmp_path / "notifications"),
    )
    app = create_app(
        settings, store=store, orchestrator=orchestrator, transcriber=FakeTranscriber()
    )
    return TestClient(app)


def wait_for_job(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/media/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.05)
    raise TimeoutError("媒體工作逾時未完成")


# ---- 純文字 ----

def test_post_meeting_returns_analysis(client):
    resp = client.post(
        "/api/meetings",
        json={"text": "鈺翔下週一交 prompt", "meeting_date": "2026-07-12"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["meeting_id"]
    assert body["analysis"]["meeting"]["title"] == "專題進度會議"
    assert "email_draft" in body["notifications"]


def test_post_empty_text_returns_400(client):
    resp = client.post("/api/meetings", json={"text": "   "})
    assert resp.status_code == 400


def test_tasks_listed_after_analysis(client):
    client.post("/api/meetings", json={"text": "開會內容"})
    tasks = client.get("/api/tasks").json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["task"] == "完成 Prompt 初版"


def test_meetings_listed(client):
    client.post("/api/meetings", json={"text": "開會內容"})
    meetings = client.get("/api/meetings").json()["meetings"]
    assert len(meetings) == 1


# ---- 檔案上傳 ----

def test_media_upload_and_poll_to_done(client):
    resp = client.post(
        "/api/media",
        files={"file": ("meeting.wav", io.BytesIO(b"RIFF-fake-wav"), "audio/wav")},
        data={"meeting_date": "2026-07-12"},
    )
    assert resp.status_code == 200
    job = wait_for_job(client, resp.json()["job_id"])
    assert job["status"] == "done"
    assert "Kevin" in job["transcript"]
    assert job["result"]["meeting_id"]


def test_media_unknown_job_404(client):
    assert client.get("/api/media/nope").status_code == 404


def test_media_bad_date_400(client):
    resp = client.post(
        "/api/media",
        files={"file": ("m.wav", io.BytesIO(b"x"), "audio/wav")},
        data={"meeting_date": "下週五"},
    )
    assert resp.status_code == 400


# ---- 即時聆聽 ----

def test_live_full_flow(client):
    sid = client.post("/api/live/start").json()["session_id"]

    resp = client.post(
        f"/api/live/{sid}/chunk",
        files={"file": ("chunk.webm", io.BytesIO(b"fake-webm"), "audio/webm")},
    )
    assert resp.status_code == 200
    assert "Kevin" in resp.json()["transcript"]

    resp = client.post(f"/api/live/{sid}/finish", json={"meeting_date": "2026-07-12"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["analysis"]["meeting"]["title"] == "專題進度會議"
    assert "transcript" in body


def test_live_unknown_session_404(client):
    resp = client.post(
        "/api/live/nope/chunk",
        files={"file": ("c.webm", io.BytesIO(b"x"), "audio/webm")},
    )
    assert resp.status_code == 404
    assert client.post("/api/live/nope/finish").status_code == 404


def test_live_finish_without_speech_400(client):
    sid = client.post("/api/live/start").json()["session_id"]
    assert client.post(f"/api/live/{sid}/finish").status_code == 400


def test_live_chunk_after_finish_400(client):
    sid = client.post("/api/live/start").json()["session_id"]
    client.post(
        f"/api/live/{sid}/chunk",
        files={"file": ("c.webm", io.BytesIO(b"x"), "audio/webm")},
    )
    client.post(f"/api/live/{sid}/finish")
    resp = client.post(
        f"/api/live/{sid}/chunk",
        files={"file": ("c2.webm", io.BytesIO(b"y"), "audio/webm")},
    )
    assert resp.status_code == 400


# ---- 其他 ----

def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert "ffmpeg" in body
    assert body["gemini_key_set"] is False


def test_index_serves_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
