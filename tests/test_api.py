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


def test_live_chunk_transcribe_error_returns_502_with_reason(tmp_path):
    """轉錄後端（如 Gemini 額度爆掉）失敗時，前端要能看到真正原因，而非不明 500。"""

    class BrokenTranscriber:
        device = "gemini"
        model_size = "fake"

        def transcribe(self, path, on_progress=None):
            raise RuntimeError("429 quota exceeded")

    settings = Settings(gemini_api_key=None, data_dir=tmp_path)
    app = create_app(settings, transcriber=BrokenTranscriber())
    c = TestClient(app)

    sid = c.post("/api/live/start").json()["session_id"]
    resp = c.post(
        f"/api/live/{sid}/chunk",
        files={"file": ("c.webm", io.BytesIO(b"x"), "audio/webm")},
    )
    assert resp.status_code == 502
    assert "quota" in resp.json()["detail"]

    # session 不應因單段失敗而壞掉：之後的段仍可繼續
    assert c.post(f"/api/live/{sid}/finish").status_code == 400  # 沒有成功內容


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


# ---- 任務管理 ----

def make_meeting(client) -> str:
    resp = client.post("/api/meetings", json={"text": "鈺翔下週一交 prompt"})
    return resp.json()["meeting_id"]


def test_patch_task_updates_status_and_owner(client):
    make_meeting(client)
    task = client.get("/api/tasks").json()["tasks"][0]
    assert task["status"] == "todo"

    resp = client.patch(f"/api/tasks/{task['id']}", json={"status": "done", "owner": "Kevin"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"
    assert client.get("/api/tasks").json()["tasks"][0]["owner"] == "Kevin"


def test_patch_task_rejects_unknown_fields_and_bad_status(client):
    make_meeting(client)
    task_id = client.get("/api/tasks").json()["tasks"][0]["id"]
    assert client.patch(f"/api/tasks/{task_id}", json={"hacked": "yes"}).status_code == 400
    assert client.patch(f"/api/tasks/{task_id}", json={"status": "??"}).status_code == 400
    assert client.patch("/api/tasks/nope", json={"status": "done"}).status_code == 404


def test_delete_task(client):
    make_meeting(client)
    task_id = client.get("/api/tasks").json()["tasks"][0]["id"]
    assert client.delete(f"/api/tasks/{task_id}").status_code == 200
    assert client.get("/api/tasks").json()["tasks"] == []
    assert client.delete(f"/api/tasks/{task_id}").status_code == 404


def test_export_tasks_csv(client):
    make_meeting(client)
    resp = client.get("/api/export/tasks.csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    body = resp.content.decode("utf-8-sig")
    assert "完成 Prompt 初版" in body
    assert "王鈺翔" in body


def test_meeting_markdown_report(client):
    meeting_id = make_meeting(client)
    resp = client.get(f"/api/meetings/{meeting_id}/report.md")
    assert resp.status_code == 200
    body = resp.text
    assert "# 專題進度會議" in body
    assert "完成 Prompt 初版" in body
    assert client.get("/api/meetings/nope/report.md").status_code == 404


def test_reminders_endpoint_scans_tasks_and_pending_items(client):
    make_meeting(client)
    task_id = client.get("/api/tasks").json()["tasks"][0]["id"]
    # 把期限改成過去 → 必為逾期，不依賴測試執行當天的日期
    client.patch(f"/api/tasks/{task_id}", json={"due_date": "2000-01-01"})

    body = client.get("/api/reminders").json()
    assert body["generated_at"]
    [r] = body["reminders"]
    assert r["kind"] == "overdue"
    assert "完成 Prompt 初版" in r["message"]
    # 會議裡的未決事項 → 追問草稿
    assert any("要不要支援英文介面" in f["topic"] for f in body["followups"])


def test_usage_endpoint_counts_analyses(client):
    assert client.get("/api/usage").json()["total"] == {}
    make_meeting(client)
    usage = client.get("/api/usage").json()
    assert usage["total"]["analysis"] == 1
    assert usage["today"]["analysis"] == 1


# ---- RAG 跨會議問答 ----

def test_ask_empty_question_400(client):
    assert client.post("/api/ask", json={"question": "   "}).status_code == 400


def test_ask_with_no_meetings_answers_gracefully(client):
    """空資料庫不需要金鑰也不觸網，直接回覆「還沒有紀錄」。"""
    body = client.post("/api/ask", json={"question": "上次開會說了什麼？"}).json()
    assert "沒有" in body["answer"]
    assert body["sources"] == []


def test_ask_with_fake_agent_returns_answer_and_counts_usage(tmp_path):
    class FakeAsk:
        def ask(self, question):
            return {"answer": f"回答：{question}", "sources": [{"meeting_id": "m1"}]}

    settings = Settings(gemini_api_key=None, data_dir=tmp_path)
    app = create_app(settings, transcriber=FakeTranscriber(), ask_agent=FakeAsk())
    c = TestClient(app)

    body = c.post("/api/ask", json={"question": "API 誰負責？"}).json()
    assert body["answer"] == "回答：API 誰負責？"
    assert c.get("/api/usage").json()["total"]["ask"] == 1


def test_ask_backend_failure_returns_502(tmp_path):
    class BrokenAsk:
        def ask(self, question):
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

    settings = Settings(gemini_api_key=None, data_dir=tmp_path)
    app = create_app(settings, transcriber=FakeTranscriber(), ask_agent=BrokenAsk())
    resp = TestClient(app).post("/api/ask", json={"question": "嗨"})
    assert resp.status_code == 502
    assert "RESOURCE_EXHAUSTED" in resp.json()["detail"]


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
