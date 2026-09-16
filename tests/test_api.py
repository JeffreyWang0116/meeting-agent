"""FastAPI 端點整合測試。

Gemini 以假 generate 取代、Whisper 以假 transcriber 取代，
其餘（store、live session、media job、pipeline）全部走真實程式碼。
"""
import io
import os
import time

import pytest
from fastapi.testclient import TestClient

from app.agents.decision_agent import DecisionAgent
from app.agents.executor_agent import ExecutorAgent
from app.agents.notifier_agent import NotifierAgent
from app.agents.parser_agent import ParserAgent
from app.config import Settings
from app.main import asset_version, create_app
from app.orchestrator import Orchestrator
from app.stores.base import DEFAULT_USER
from app.stores.local_store import LocalJsonStore
from tests.test_decision import valid_json


class FakeTranscriber:
    device = "cpu"
    model_size = "fake"

    def transcribe(self, path, on_progress=None, user=None):
        if on_progress:
            on_progress(1.0, "假逐字稿")
        return "Kevin 說週五要 demo，志明負責 prompt。"


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
        json={"text": "志明下週一交 prompt", "meeting_date": "2026-07-12"},
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


def test_meeting_kind_saved_and_unknown_rejected(client):
    resp = client.post("/api/meetings", json={"text": "講座內容", "kind": "講座"})
    assert resp.status_code == 200
    assert client.get("/api/meetings").json()["meetings"][0]["kind"] == "講座"
    # 不在清單內的種類要擋下來
    assert client.post("/api/meetings", json={"text": "x", "kind": "怪種類"}).status_code == 400


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


def test_live_session_lost_can_fall_back_to_text_analysis(client):
    """聆聽 session 遺失後的救援路徑所依賴的契約。

    session 只存在記憶體，行程一重啟就永遠找不回來，再怎麼重試 finish 都是 404。
    前端因此保留一份逐字稿副本，看到 404 就改走純文字分析把整場救回來。
    這裡釘住它依賴的兩件事：finish 對死掉的 session 回的是 404（不是 400/500，
    前端靠這個碼分辨「資料不存在」與「暫時性故障」），且純文字分析會回傳
    transcript 供結果畫面顯示。改動任一個都會讓那條退路無聲失效。
    """
    assert client.post("/api/live/nope/finish").status_code == 404

    resp = client.post(
        "/api/meetings",
        json={"text": "[0:05] 講者A：志明下週一交 prompt", "meeting_date": "2026-07-12"},
    )
    assert resp.status_code == 200
    assert resp.json()["transcript"]


def test_live_finish_without_speech_400(client):
    sid = client.post("/api/live/start").json()["session_id"]
    assert client.post(f"/api/live/{sid}/finish").status_code == 400


def test_live_chunk_transcribe_error_returns_502_with_reason(tmp_path):
    """轉錄後端（如 Gemini 額度爆掉）失敗時，前端要能看到真正原因，而非不明 500。"""

    class BrokenTranscriber:
        device = "gemini"
        model_size = "fake"

        def transcribe(self, path, on_progress=None, user=None):
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
    resp = client.post("/api/meetings", json={"text": "志明下週一交 prompt"})
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


def test_create_manual_task_and_validation(client):
    r = client.post("/api/tasks", json={"task": "買便當", "priority": "low"})
    assert r.status_code == 200
    body = r.json()
    assert body["task"] == "買便當"
    assert body["status"] == "todo"
    assert body["meeting_id"] is None
    assert any(t["task"] == "買便當" for t in client.get("/api/tasks").json()["tasks"])
    # 驗證：空名稱、非法優先級、非法日期都要擋
    assert client.post("/api/tasks", json={"task": "  "}).status_code == 400
    assert client.post("/api/tasks", json={"task": "x", "priority": "urgent"}).status_code == 400
    assert client.post("/api/tasks", json={"task": "x", "due_date": "下週五"}).status_code == 400


def test_patch_task_rejects_bad_due_date(client):
    make_meeting(client)
    tid = client.get("/api/tasks").json()["tasks"][0]["id"]
    assert client.patch(f"/api/tasks/{tid}", json={"due_date": "下週五"}).status_code == 400
    assert client.patch(f"/api/tasks/{tid}", json={"due_date": None}).status_code == 200
    assert client.patch(f"/api/tasks/{tid}", json={"due_date": "2026-09-01"}).status_code == 200


def test_patch_meeting_rejects_bad_date_and_attendees(client):
    mid = make_meeting(client)
    assert client.patch(f"/api/meetings/{mid}", json={"date": "not-a-date"}).status_code == 400
    assert client.patch(f"/api/meetings/{mid}", json={"tags": [1, 2]}).status_code == 400
    assert client.patch(f"/api/meetings/{mid}", json={"attendees": "x"}).status_code == 400
    assert client.patch(f"/api/meetings/{mid}", json={"attendees": ["Amy"]}).status_code == 200
    assert client.get(f"/api/meetings/{mid}").json()["meeting"]["attendees"] == ["Amy"]


def test_backup_and_restore_roundtrip(client):
    make_meeting(client)
    dump = client.get("/api/backup").json()
    assert dump["meetings"] and dump["tasks"]

    mid = dump["meetings"][0]["id"]
    client.delete(f"/api/meetings/{mid}")
    assert client.get("/api/meetings").json()["meetings"] == []

    r = client.post("/api/restore", json=dump)
    assert r.status_code == 200
    assert r.json()["restored"]["meetings"] == 1
    assert len(client.get("/api/meetings").json()["meetings"]) == 1
    assert len(client.get("/api/tasks").json()["tasks"]) == 1
    # 格式不對要擋
    assert client.post("/api/restore", json={"foo": 1}).status_code == 400


def test_parse_iso_date_or_none_is_typeerror_safe():
    from datetime import date

    from app.main import _parse_iso_date_or_none

    assert _parse_iso_date_or_none(None) is None  # 不會拋 TypeError
    assert _parse_iso_date_or_none("garbage") is None
    assert _parse_iso_date_or_none("2026-07-12") == date(2026, 7, 12)


def test_export_tasks_csv(client):
    make_meeting(client)
    resp = client.get("/api/export/tasks.csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    body = resp.content.decode("utf-8-sig")
    assert "完成 Prompt 初版" in body
    assert "陳志明" in body


def test_get_meeting_detail_includes_transcript(client):
    meeting_id = make_meeting(client)
    body = client.get(f"/api/meetings/{meeting_id}").json()
    assert body["id"] == meeting_id
    assert body["meeting"]["title"] == "專題進度會議"
    assert body["transcript"]  # 詳情要含逐字稿全文
    assert client.get("/api/meetings/nope").status_code == 404


def test_meeting_tags_from_analysis_and_editable(client):
    meeting_id = make_meeting(client)
    # 分析時 AI 建議的標籤直接入庫
    assert client.get("/api/meetings").json()["meetings"][0]["tags"] == ["專題", "進度會議"]
    # 可自訂修改
    resp = client.patch(f"/api/meetings/{meeting_id}", json={"tags": ["客戶", "週會"]})
    assert resp.status_code == 200
    assert client.get(f"/api/meetings/{meeting_id}").json()["tags"] == ["客戶", "週會"]
    assert client.patch(f"/api/meetings/{meeting_id}", json={"tags": "不是陣列"}).status_code == 400


def test_patch_meeting_updates_title_and_transcript(client):
    meeting_id = make_meeting(client)
    resp = client.patch(
        f"/api/meetings/{meeting_id}",
        json={"title": "新標題", "summary": "新摘要", "transcript": "新逐字稿"},
    )
    assert resp.status_code == 200
    body = client.get(f"/api/meetings/{meeting_id}").json()
    assert body["meeting"]["title"] == "新標題"
    assert body["meeting"]["summary"] == "新摘要"
    assert body["transcript"] == "新逐字稿"
    # 不允許的欄位要擋
    assert client.patch(f"/api/meetings/{meeting_id}", json={"id": "hack"}).status_code == 400
    assert client.patch("/api/meetings/nope", json={"title": "x"}).status_code == 404


def test_delete_meeting_removes_meeting_and_tasks(client):
    meeting_id = make_meeting(client)
    assert client.get("/api/tasks").json()["tasks"]
    assert client.delete(f"/api/meetings/{meeting_id}").status_code == 200
    assert client.get(f"/api/meetings/{meeting_id}").status_code == 404
    assert client.get("/api/tasks").json()["tasks"] == []
    assert client.delete(f"/api/meetings/{meeting_id}").status_code == 404


def test_reanalyze_meeting_updates_analysis_and_replaces_tasks(client):
    meeting_id = make_meeting(client)
    old_task_ids = {t["id"] for t in client.get("/api/tasks").json()["tasks"]}
    # 先編輯逐字稿，再重新分析（假 LLM 回固定 JSON，重點是流程與資料替換）
    client.patch(f"/api/meetings/{meeting_id}", json={"transcript": "改過的內容"})

    resp = client.post(f"/api/meetings/{meeting_id}/reanalyze")
    assert resp.status_code == 200
    body = resp.json()
    assert body["meeting_id"] == meeting_id  # 不會生出新會議
    assert body["analysis"]["meeting"]["title"] == "專題進度會議"
    assert "email_draft" in body["notifications"]

    tasks = client.get("/api/tasks").json()["tasks"]
    assert len(tasks) == 1
    assert {t["id"] for t in tasks}.isdisjoint(old_task_ids)  # 任務整批換新
    assert client.post("/api/meetings/nope/reanalyze").status_code == 404


def test_reanalyze_without_transcript_400(client):
    meeting_id = make_meeting(client)
    client.patch(f"/api/meetings/{meeting_id}", json={"transcript": ""})
    assert client.post(f"/api/meetings/{meeting_id}/reanalyze").status_code == 400


def test_replace_term_uniformly_and_adds_glossary(client):
    meeting_id = make_meeting(client)  # 逐字稿："志明下週一交 prompt"
    resp = client.post(
        f"/api/meetings/{meeting_id}/replace-term",
        json={"old": "志明", "new": "玉翔", "add_to_glossary": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["replaced"] == 1
    assert body["glossary_added"] is True
    assert body["meeting"]["transcript"] == "玉翔下週一交 prompt"
    # 真的寫回了，且新詞進了詞彙表（事後修正兼事前預防）
    assert client.get(f"/api/meetings/{meeting_id}").json()["transcript"] == "玉翔下週一交 prompt"
    assert any(t["term"] == "玉翔" for t in client.get("/api/glossary").json()["terms"])


def test_replace_term_replaces_every_occurrence(client):
    meeting_id = make_meeting(client)
    client.patch(f"/api/meetings/{meeting_id}", json={"transcript": "涵式 A 涵式 B 涵式"})
    resp = client.post(
        f"/api/meetings/{meeting_id}/replace-term", json={"old": "涵式", "new": "函式"}
    )
    assert resp.status_code == 200
    assert resp.json()["replaced"] == 3
    assert client.get(f"/api/meetings/{meeting_id}").json()["transcript"] == "函式 A 函式 B 函式"


def test_replace_term_not_found_is_noop(client):
    meeting_id = make_meeting(client)
    resp = client.post(
        f"/api/meetings/{meeting_id}/replace-term",
        json={"old": "不存在的詞", "new": "x", "add_to_glossary": True},
    )
    assert resp.status_code == 200
    assert resp.json()["replaced"] == 0
    assert resp.json()["glossary_added"] is False  # 沒替換到就不動詞彙表
    assert client.get(f"/api/meetings/{meeting_id}").json()["transcript"] == "志明下週一交 prompt"
    assert client.get("/api/glossary").json()["terms"] == []


def test_replace_term_validation_and_missing_meeting(client):
    meeting_id = make_meeting(client)
    assert client.post(
        f"/api/meetings/{meeting_id}/replace-term", json={"old": "  ", "new": "x"}
    ).status_code == 400  # 空原詞要擋
    assert client.post(
        "/api/meetings/nope/replace-term", json={"old": "a", "new": "b"}
    ).status_code == 404


def test_replace_term_within_time_window(client):
    meeting_id = make_meeting(client)
    client.patch(
        f"/api/meetings/{meeting_id}",
        json={"transcript": "[0:05] 講者A：涵式\n[1:30] 講者B：涵式\n[2:10] 講者A：涵式"},
    )
    # 只替換 1:00~2:00 之間 → 只換中間那一處
    resp = client.post(
        f"/api/meetings/{meeting_id}/replace-term",
        json={"old": "涵式", "new": "函式", "start": "1:00", "end": "2:00"},
    )
    assert resp.status_code == 200
    assert resp.json()["replaced"] == 1
    assert client.get(f"/api/meetings/{meeting_id}").json()["transcript"] == (
        "[0:05] 講者A：涵式\n[1:30] 講者B：函式\n[2:10] 講者A：涵式"
    )


def test_replace_term_rejects_bad_time_and_inverted_range(client):
    meeting_id = make_meeting(client)
    assert client.post(
        f"/api/meetings/{meeting_id}/replace-term",
        json={"old": "a", "new": "b", "start": "亂打"},
    ).status_code == 400
    assert client.post(
        f"/api/meetings/{meeting_id}/replace-term",
        json={"old": "a", "new": "b", "start": "5:00", "end": "1:00"},
    ).status_code == 400


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


def test_ask_passes_meeting_ids_scope(tmp_path):
    captured = {}

    class ScopeAsk:
        def ask(self, question, meeting_ids=None, user=DEFAULT_USER):
            captured["meeting_ids"] = meeting_ids
            captured["user"] = user
            return {"answer": "ok", "sources": []}

    settings = Settings(gemini_api_key=None, data_dir=tmp_path)
    app = create_app(settings, transcriber=FakeTranscriber(), ask_agent=ScopeAsk())
    c = TestClient(app)
    c.post("/api/ask", json={"question": "誰負責？", "meeting_ids": ["m1", "m2"]})
    assert captured["meeting_ids"] == ["m1", "m2"]
    c.post("/api/ask", json={"question": "誰負責？"})
    assert captured["meeting_ids"] is None
    # 檢索範圍必須綁在使用者身上，否則接上登入後會問到別人的會議
    assert captured["user"] == DEFAULT_USER


def test_ask_with_fake_agent_returns_answer_and_counts_usage(tmp_path):
    class FakeAsk:
        def ask(self, question, meeting_ids=None, user=DEFAULT_USER):
            return {"answer": f"回答：{question}", "sources": [{"meeting_id": "m1"}]}

    settings = Settings(gemini_api_key=None, data_dir=tmp_path)
    app = create_app(settings, transcriber=FakeTranscriber(), ask_agent=FakeAsk())
    c = TestClient(app)

    body = c.post("/api/ask", json={"question": "API 誰負責？"}).json()
    assert body["answer"] == "回答：API 誰負責？"
    assert c.get("/api/usage").json()["total"]["ask"] == 1


def test_ask_backend_failure_returns_502(tmp_path):
    class BrokenAsk:
        def ask(self, question, meeting_ids=None, user=DEFAULT_USER):
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

    settings = Settings(gemini_api_key=None, data_dir=tmp_path)
    app = create_app(settings, transcriber=FakeTranscriber(), ask_agent=BrokenAsk())
    resp = TestClient(app).post("/api/ask", json={"question": "嗨"})
    assert resp.status_code == 502
    assert "RESOURCE_EXHAUSTED" in resp.json()["detail"]


def test_meeting_events_ics_download(client):
    meeting_id = make_meeting(client)  # 任務含 due_date 2026-07-20
    resp = client.get(f"/api/meetings/{meeting_id}/events.ics")
    assert resp.status_code == 200
    assert "text/calendar" in resp.headers["content-type"]
    body = resp.text
    assert "BEGIN:VCALENDAR" in body
    assert "BEGIN:VEVENT" in body
    assert "DTSTART;VALUE=DATE:20260720" in body
    assert "DTEND;VALUE=DATE:20260721" in body  # 全天事件 end 是隔天（exclusive）
    assert "完成 Prompt 初版" in body
    assert client.get("/api/meetings/nope/events.ics").status_code == 404


# ---- 關鍵字搜尋 ----

def test_keyword_search_finds_meetings_with_snippet(client):
    make_meeting(client)  # 逐字稿 = 「志明下週一交 prompt」
    body = client.get("/api/search", params={"q": "prompt"}).json()
    assert body["hits"]
    hit = body["hits"][0]
    assert hit["title"] == "專題進度會議"
    assert "prompt" in hit["snippet"].lower()
    assert hit["meeting_id"]
    # 大小寫不敏感
    assert client.get("/api/search", params={"q": "PROMPT"}).json()["hits"]
    # 沒中就空陣列；空關鍵字要擋
    assert client.get("/api/search", params={"q": "絕不存在的字串xyz"}).json()["hits"] == []
    assert client.get("/api/search", params={"q": "  "}).status_code == 400


# ---- 翻譯 ----

def make_client_with_translator(tmp_path, translator):
    settings = Settings(gemini_api_key=None, data_dir=tmp_path)
    app = create_app(settings, transcriber=FakeTranscriber(), translator=translator)
    return TestClient(app)


def test_translate_endpoint(tmp_path):
    class FakeTranslator:
        def translate(self, text, target):
            return f"[{target}] {text}"

    c = make_client_with_translator(tmp_path, FakeTranslator())
    body = c.post("/api/translate", json={"text": "大家好", "target": "en"}).json()
    assert body["translation"] == "[en] 大家好"
    # 空字串與不支援的語言要擋
    assert c.post("/api/translate", json={"text": " ", "target": "en"}).status_code == 400
    assert c.post("/api/translate", json={"text": "hi", "target": "fr"}).status_code == 400


def test_translate_backend_failure_returns_502(tmp_path):
    class BrokenTranslator:
        def translate(self, text, target):
            raise RuntimeError("429 quota")

    c = make_client_with_translator(tmp_path, BrokenTranslator())
    resp = c.post("/api/translate", json={"text": "hi", "target": "en"})
    assert resp.status_code == 502


def test_live_start_accepts_translate_to_and_chunk_returns_translation(tmp_path):
    class FakeTranslator:
        def translate(self, text, target):
            return f"[{target}] {text}"

    c = make_client_with_translator(tmp_path, FakeTranslator())
    sid = c.post("/api/live/start", json={"translate_to": "en"}).json()["session_id"]
    r = c.post(
        f"/api/live/{sid}/chunk",
        files={"file": ("c.webm", io.BytesIO(b"x"), "audio/webm")},
    ).json()
    assert r["translation"].startswith("[en] ")
    # 不支援的目標語言要擋
    assert c.post("/api/live/start", json={"translate_to": "fr"}).status_code == 400


# ---- 自訂詞彙 ----

def test_glossary_roundtrip_and_validation(client):
    assert client.get("/api/glossary").json() == {"terms": []}

    resp = client.put(
        "/api/glossary",
        json={"terms": [{"term": "林佳蓉", "note": "人名"}, {"term": "TaskHub"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["terms"] == [
        {"term": "林佳蓉", "note": "人名"},
        {"term": "TaskHub", "note": ""},
    ]
    assert client.get("/api/glossary").json()["terms"][0]["term"] == "林佳蓉"
    # 空詞彙要擋
    assert client.put("/api/glossary", json={"terms": [{"term": "  "}]}).status_code == 400


def test_glossary_survives_backup_without_the_removed_person_flag(client):
    """「人名」勾選已移除：舊前端送來的 person 被丟掉，詞彙本身照樣跟著備份走。"""
    client.put(
        "/api/glossary",
        json={"terms": [{"term": "林佳蓉", "person": True}, {"term": "TaskHub"}]},
    )
    terms = client.get("/api/backup").json()["glossary"]
    assert terms == [{"term": "林佳蓉", "note": ""}, {"term": "TaskHub", "note": ""}]


# ---- 其他 ----

def test_gemini_engine_uses_transcribe_model_not_analysis_model(tmp_path):
    """轉錄用高額度輕量模型、分析用聰明模型：兩者必須各走各的設定。"""
    settings = Settings(
        gemini_api_key="k",
        transcribe_engine="gemini",
        gemini_model="gemini-3.5-flash",
        transcribe_model="gemini-3.5-flash-lite",
        data_dir=tmp_path,
    )
    app = create_app(settings)
    body = TestClient(app).get("/api/health").json()
    assert body["whisper_model"] == "gemini-3.5-flash-lite"  # 轉錄模型
    assert body["gemini_model"] == "gemini-3.5-flash"  # 分析模型不受影響


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert "ffmpeg" in body
    assert body["gemini_key_set"] is False


def test_index_serves_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_index_stamps_css_and_js_with_a_version(client):
    """瀏覽器在我們加上 Cache-Control 之前存過的舊 CSS/JS，會依啟發式規則自認
    新鮮、根本不回來問伺服器。標頭救不了已經存進去的那一份，只有換掉 URL 才行。"""
    html = client.get("/").text
    assert '"/static/style.css?v=' in html
    assert '"/static/js/main.js?v=' in html
    assert '"/static/style.css"' not in html
    # 雪碧圖是新檔、不可能有舊快取，且 app.js 在執行期也會組出同樣的網址，
    # 加了版本反而變成兩個 URL 各下載一次
    assert "/static/icons.svg?" not in html


def test_asset_version_changes_when_a_file_changes(tmp_path):
    """版本取 style.css 與 js/ 底下每支模組裡最新的 mtime。部署／編輯都只會讓
    檔案變新，所以只要有任何一個被動過，版本就會跟著換。

    前端早就從單一 app.js 拆成 js/*.js；這支測試原本還在根目錄建 app.js，
    第二段又拿改 CSS 之前的版本比，JS 模組改了版本換不換根本沒被驗到。"""
    (tmp_path / "style.css").write_text("a", encoding="utf-8")
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "main.js").write_text("b", encoding="utf-8")
    (tmp_path / "js" / "transcript.js").write_text("c", encoding="utf-8")
    base = time.time()
    for path in (tmp_path / "style.css", *(tmp_path / "js").iterdir()):
        os.utime(path, (base, base))
    before = asset_version(tmp_path)

    os.utime(tmp_path / "style.css", (base + 60, base + 60))
    after_css = asset_version(tmp_path)
    assert after_css != before

    # 只動其中一支 JS 模組，版本也要跟著換（比的是改 CSS 之後的版本）
    os.utime(tmp_path / "js" / "transcript.js", (base + 120, base + 120))
    assert asset_version(tmp_path) not in (before, after_css, "")


def test_asset_version_survives_missing_files(tmp_path):
    assert asset_version(tmp_path)  # 不該炸，也不該回空字串


def test_pwa_manifest_sw_and_icon_served(client):
    resp = client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp.json()["name"] == "會議助手"
    assert resp.json()["icons"]

    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]

    assert client.get("/static/icon.svg").status_code == 200


# ---- 預期外故障（缺套件、SDK 改版…）要回看得懂的 502，不是 500 stack trace ----

def test_unexpected_analysis_failure_returns_clean_502(tmp_path):
    from app.agents.decision_agent import DecisionAgent
    from app.agents.executor_agent import ExecutorAgent
    from app.agents.notifier_agent import NotifierAgent
    from app.agents.parser_agent import ParserAgent
    from app.config import Settings
    from app.orchestrator import Orchestrator
    from app.stores.local_store import LocalJsonStore

    def boom(prompt):
        raise ModuleNotFoundError("No module named 'google'")

    store = LocalJsonStore(tmp_path / "db.json")
    app = create_app(
        Settings(gemini_api_key=None, data_dir=tmp_path),
        store=store,
        orchestrator=Orchestrator(
            parser=ParserAgent(),
            decision=DecisionAgent(generate=boom),
            executor=ExecutorAgent(store),
            notifier=NotifierAgent(tmp_path / "n"),
        ),
    )
    resp = TestClient(app).post("/api/meetings", json={"text": "測試"})
    assert resp.status_code == 502
    assert "ModuleNotFoundError" in resp.json()["detail"]


# ---- 上傳防護：大小上限與檔案型別 ----
# 原本 /api/media 直接把上傳串進磁碟，沒有任何上限。免費層雲端磁碟只有幾百 MB，
# 一個手滑的大檔就能寫爆——寫爆之後連 db.json 都存不進去，整個服務停擺。

@pytest.fixture
def tiny_limit_client(tmp_path):
    """上限縮到 1MB 的用戶端：測試不必真的產生 500MB 資料。"""
    settings = Settings(gemini_api_key=None, data_dir=tmp_path, max_upload_mb=1)
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
    return TestClient(app), tmp_path


def test_media_upload_rejects_file_over_limit(tiny_limit_client):
    client, tmp_path = tiny_limit_client
    oversized = io.BytesIO(b"0" * (2 * 1024 * 1024))

    resp = client.post(
        "/api/media", files={"file": ("huge.wav", oversized, "audio/wav")}
    )

    assert resp.status_code == 413
    assert "MB" in resp.json()["detail"]
    # 半截檔案不能留在磁碟上——那正是要防的事
    assert list((tmp_path / "tmp" / "uploads").glob("*")) == []


def test_media_upload_accepts_file_within_limit(tiny_limit_client):
    client, _ = tiny_limit_client
    resp = client.post(
        "/api/media",
        files={"file": ("ok.wav", io.BytesIO(b"0" * 1024), "audio/wav")},
    )
    assert resp.status_code == 200
    assert wait_for_job(client, resp.json()["job_id"])["status"] == "done"


def test_media_upload_rejects_non_media_file(client):
    resp = client.post(
        "/api/media", files={"file": ("payload.exe", io.BytesIO(b"MZ"), "application/octet-stream")}
    )
    assert resp.status_code == 400
    assert "格式" in resp.json()["detail"]


def test_media_upload_rejects_empty_file(client):
    resp = client.post(
        "/api/media", files={"file": ("empty.wav", io.BytesIO(b""), "audio/wav")}
    )
    assert resp.status_code == 400


def test_live_chunk_rejects_oversized_chunk(tiny_limit_client):
    """即時聆聽的每段音訊是整段讀進記憶體的，上限比落地檔案更該守。"""
    client, _ = tiny_limit_client
    sid = client.post("/api/live/start").json()["session_id"]

    resp = client.post(
        f"/api/live/{sid}/chunk",
        files={"file": ("c.webm", io.BytesIO(b"0" * (2 * 1024 * 1024)), "audio/webm")},
    )

    assert resp.status_code == 413


def test_nothing_can_write_names_into_the_glossary_behind_the_users_back(client):
    """詞彙表只收使用者在管理介面自己輸入的詞。

    原本在歷史會議手動改講者名會順手把姓名寫進詞彙表，讓之後每一場的轉錄與
    分析 prompt 都帶著它。使用者以為自己只是修正那一場的顯示，不會想到這會
    影響往後所有會議——而且那個名字是怎麼進到詞彙表的，事後完全查不出來。

    姓名要進詞彙表只剩一條路：設定 → 自訂詞彙 → 手動新增。
    """
    client.put("/api/glossary", json={"terms": [{"term": "TaskHub", "note": "產品"}]})

    assert client.post("/api/glossary/persons", json={"names": ["陳大文"]}).status_code == 404

    terms = client.get("/api/glossary").json()["terms"]
    assert [t["term"] for t in terms] == ["TaskHub"]


# ---- 預錄聲音辨識人（選用功能）----

def test_enroll_returns_the_registered_count(client):
    sid = client.post("/api/live/start").json()["session_id"]
    resp = client.post(
        f"/api/live/{sid}/enroll",
        files={"file": ("s.webm", io.BytesIO(b"voice"), "audio/webm")},
        data={"name": "王小明"},
    )
    assert resp.status_code == 200
    assert resp.json()["enrolled"] == 1


def test_enroll_unknown_session_404(client):
    resp = client.post(
        "/api/live/nope/enroll",
        files={"file": ("s.webm", io.BytesIO(b"x"), "audio/webm")},
        data={"name": "王小明"},
    )
    assert resp.status_code == 404


def test_enroll_rejects_unsafe_name_400(client):
    """姓名最後會寫進逐字稿的講者欄，含冒號會造出假標籤。"""
    sid = client.post("/api/live/start").json()["session_id"]
    resp = client.post(
        f"/api/live/{sid}/enroll",
        files={"file": ("s.webm", io.BytesIO(b"x"), "audio/webm")},
        data={"name": "王小明：主席"},
    )
    assert resp.status_code == 400


def _voice_app(tmp_path, transcriber, matcher):
    """建一個「只有聲紋比對是真的」的 app：LLM 全部以假 generate 取代。"""
    from app.transcription.live_session import LiveSessionManager

    settings = Settings(gemini_api_key=None, data_dir=tmp_path)
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(generate=lambda prompt: valid_json()),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(tmp_path / "notifications"),
    )
    return TestClient(create_app(
        settings,
        store=store,
        orchestrator=orchestrator,
        transcriber=transcriber,
        live_manager=LiveSessionManager(
            transcriber, tmp_path / "live", voice_matcher=matcher
        ),
    ))


def test_live_flow_without_enrollment_never_touches_the_matcher(tmp_path):
    """沒用這個功能的人，走的路徑要與它不存在時完全相同——連比對都不該發生。"""
    calls = []

    class SpyMatcher:
        def match(self, enrollments, evidence):
            calls.append(1)
            return {}

    c = _voice_app(tmp_path, FakeTranscriber(), SpyMatcher())
    sid = c.post("/api/live/start").json()["session_id"]
    c.post(
        f"/api/live/{sid}/chunk",
        files={"file": ("c.webm", io.BytesIO(b"x"), "audio/webm")},
    )
    assert c.post(f"/api/live/{sid}/finish").status_code == 200
    assert calls == []


def test_enrolled_voices_rename_the_speaker_labels(tmp_path):
    """預錄樣本的姓名是使用者自己填的，比對到就直接換上——這是唯一會自動填姓名的路徑。"""

    class LabelledTranscriber:
        device = "cpu"
        model_size = "fake"

        def transcribe(self, path, on_progress=None, hint=None, user=None):
            return "[0:01] 講者A：週五要 demo"

    class FixedMatcher:
        def match(self, enrollments, evidence):
            return {"講者A": "王小明"}

    c = _voice_app(tmp_path, LabelledTranscriber(), FixedMatcher())
    sid = c.post("/api/live/start").json()["session_id"]
    assert c.post(
        f"/api/live/{sid}/enroll",
        files={"file": ("s.webm", io.BytesIO(b"voice"), "audio/webm")},
        data={"name": "王小明"},
    ).status_code == 200
    c.post(
        f"/api/live/{sid}/chunk",
        files={"file": ("c.webm", io.BytesIO(b"x"), "audio/webm")},
        data={"offset": "0"},
    )
    body = c.post(f"/api/live/{sid}/finish", json={"meeting_date": "2026-07-12"}).json()
    assert "王小明：" in body["transcript"]
    assert "講者A" not in body["transcript"]
    assert body["speaker_names"] == [{"label": "講者A", "name": "王小明", "count": 1}]


# ---- 即時聆聽：預錄聲音辨識人 ----

class LabelledTranscriber:
    """回傳帶時間戳與講者代號的假逐字稿，讓聲紋比對的證據挑得出來。"""

    device = "cpu"
    model_size = "fake"

    def __init__(self, texts=None):
        self.texts = list(texts or [])

    def transcribe(self, path, on_progress=None, hint=None, user=None):
        return self.texts.pop(0) if self.texts else "[0:01] 講者A：大家好"


class FakeVoiceMatcher:
    def __init__(self, result):
        self.result = result

    def match(self, enrollments, evidence):
        return dict(self.result)


def _live_app(tmp_path, transcriber, matcher=None, on_prompt=None):
    from app.transcription.live_session import LiveSessionManager

    settings = Settings(gemini_api_key=None, data_dir=tmp_path)
    store = LocalJsonStore(tmp_path / "db.json")

    def generate(prompt):
        if on_prompt:
            on_prompt(prompt)
        return valid_json()

    orchestrator = Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(generate=generate),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(tmp_path / "notifications"),
    )
    live_manager = LiveSessionManager(
        transcriber,
        tmp_path / "live",
        voice_matcher=matcher,
        spawn=lambda fn: fn(),  # 提早比對原地執行，測試才不必跟執行緒賽跑
    )
    app = create_app(
        settings,
        store=store,
        orchestrator=orchestrator,
        transcriber=transcriber,
        live_manager=live_manager,
    )
    return TestClient(app)


def _chunk(client, sid, data=b"audio", **form):
    return client.post(
        f"/api/live/{sid}/chunk",
        files={"file": ("c.webm", io.BytesIO(data), "audio/webm")},
        data=form,
    )


def test_live_chunk_drops_the_overlapping_seconds(tmp_path):
    """前端讓相鄰兩段重疊幾秒，一句話才不會被硬切點剁成兩半。

    重疊處會被轉錄兩次，後端依絕對時間濾掉——否則逐字稿每 45 秒就出現一句重複。
    """
    tr = LabelledTranscriber(
        ["[0:00] 講者A：大家好", "[0:00] 講者A：大家好\n[0:05] 講者A：今天談排程"]
    )
    c = _live_app(tmp_path, tr)
    sid = c.post("/api/live/start").json()["session_id"]
    _chunk(c, sid, offset="0")
    body = _chunk(c, sid, offset="42", overlap="3").json()

    assert body["text"] == "[0:47] 講者A：今天談排程"
    assert body["transcript"].count("大家好") == 1


def test_live_finish_reports_who_was_recognised_and_who_was_not(tmp_path):
    """預錄了 2 位只認出 1 位時，使用者要知道是哪一位沒認出來。

    沒有這份回報，畫面上只是「有些人有名字、有些人沒有」，使用者無從判斷是
    自己樣本錄壞了，還是功能根本沒生效。
    """
    c = _live_app(
        tmp_path,
        LabelledTranscriber(["[0:01] 講者A：大家好"] * 3),
        matcher=FakeVoiceMatcher({"講者A": "王小明"}),
    )
    sid = c.post("/api/live/start").json()["session_id"]
    for name in ("王小明", "李美華"):
        assert c.post(
            f"/api/live/{sid}/enroll",
            files={"file": ("e.webm", io.BytesIO(b"sample"), "audio/webm")},
            data={"name": name},
        ).status_code == 200
    _chunk(c, sid, offset="0")

    body = c.post(f"/api/live/{sid}/finish").json()
    assert body["speakers_matched"] == {"講者A": "王小明"}
    assert body["speakers_unmatched"] == ["李美華"]


def test_retrying_the_analysis_keeps_the_voiceprint_names(tmp_path):
    """finish 會刪掉音檔，重試時已經沒有聲音可比——比對結果必須沿用快取。

    沒快取的話，第一次分析失敗、重試成功的使用者會看到整份逐字稿退回代號。
    """
    calls = []

    class CountingMatcher(FakeVoiceMatcher):
        def match(self, enrollments, evidence):
            calls.append(1)
            return super().match(enrollments, evidence)

    c = _live_app(
        tmp_path,
        LabelledTranscriber(["[0:01] 講者A：大家好"] * 3),
        matcher=CountingMatcher({"講者A": "王小明"}),
    )
    sid = c.post("/api/live/start").json()["session_id"]
    c.post(
        f"/api/live/{sid}/enroll",
        files={"file": ("e.webm", io.BytesIO(b"sample"), "audio/webm")},
        data={"name": "王小明"},
    )
    _chunk(c, sid, offset="0")

    assert c.post(f"/api/live/{sid}/finish").json()["speakers_matched"] == {
        "講者A": "王小明"
    }
    again = c.post(f"/api/live/{sid}/finish").json()  # 前端的「重試分析」
    assert again["speakers_matched"] == {"講者A": "王小明"}
    assert len(calls) == 1  # 音檔都沒了，不該再白打一次


def test_enrolled_people_are_given_to_the_analysis_as_attendees(tmp_path):
    """會前登記的人＝使用者親自指認的出席名單，分析時用得上。"""
    prompts = []
    c = _live_app(
        tmp_path,
        LabelledTranscriber(["[0:01] 講者A：大家好"] * 3),
        matcher=FakeVoiceMatcher({}),
        on_prompt=prompts.append,
    )
    sid = c.post("/api/live/start").json()["session_id"]
    c.post(
        f"/api/live/{sid}/enroll",
        files={"file": ("e.webm", io.BytesIO(b"sample"), "audio/webm")},
        data={"name": "李美華"},
    )
    _chunk(c, sid, offset="0")
    c.post(f"/api/live/{sid}/finish")

    assert prompts and "李美華" in prompts[0]


# ---- pyannote 講者分離接線 ----

def test_media_upload_uses_injected_diarizer(tmp_path):
    """釘住接線本身：Diarizer 的機制測得再完整，create_app 沒把它交給
    MediaJobManager 的話，功能照樣是關的。"""

    class RelabelDiarizer:
        def start(self, path):
            return None

        def apply(self, future, transcript):
            return "[0:00] 講者B：pyannote 重標過"

    settings = Settings(gemini_api_key=None, data_dir=tmp_path)
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(generate=lambda prompt: valid_json()),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(tmp_path / "notifications"),
    )
    app = create_app(
        settings, store=store, orchestrator=orchestrator,
        transcriber=FakeTranscriber(), diarizer=RelabelDiarizer(),
    )
    client = TestClient(app)
    resp = client.post(
        "/api/media",
        files={"file": ("meeting.wav", io.BytesIO(b"RIFF-fake-wav"), "audio/wav")},
    )
    job = wait_for_job(client, resp.json()["job_id"])
    assert job["status"] == "done"
    assert "pyannote 重標過" in job["transcript"]


def _app_with_diarizer(tmp_path, diarizer):
    settings = Settings(gemini_api_key=None, data_dir=tmp_path)
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(generate=lambda prompt: valid_json()),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(tmp_path / "notifications"),
    )
    app = create_app(
        settings, store=store, orchestrator=orchestrator,
        transcriber=FakeTranscriber(), diarizer=diarizer,
    )
    return TestClient(app)


class SessionDiarizer:
    def __init__(self):
        self.calls = []

    def start(self, path):
        return None

    def apply(self, future, transcript):
        return transcript

    def relabel_session(self, transcript, pieces, enrollments):
        self.calls.append((transcript, pieces, enrollments))
        return "[0:00] 講者B：pyannote 整場重標", {"講者B": "王小明"}


def test_live_finish_uses_diarizer_for_transcript_and_names(tmp_path):
    """釘住即時聆聽的接線：LiveSessionManager 要拿到 diarizer，live_finish 要用它的結果。"""
    diarizer = SessionDiarizer()
    client = _app_with_diarizer(tmp_path, diarizer)
    sid = client.post("/api/live/start").json()["session_id"]
    client.post(
        f"/api/live/{sid}/enroll",
        files={"file": ("enroll.webm", io.BytesIO(b"voice"), "audio/webm")},
        data={"name": "王小明"},
    )
    client.post(
        f"/api/live/{sid}/chunk",
        files={"file": ("chunk.webm", io.BytesIO(b"audio"), "audio/webm")},
        data={"offset": "0"},
    )

    body = client.post(f"/api/live/{sid}/finish").json()

    assert len(diarizer.calls) == 1
    assert "pyannote 整場重標" in body["transcript"]
    assert body["speakers_matched"] == {"講者B": "王小明"}
    assert body["speakers_unmatched"] == []


class RecordingGeminiTranscriber:
    """取代 create_app 裡建的 GeminiTranscriber，只記下收到的參數。"""

    instances: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.device = "gemini"
        self.model_size = kwargs.get("model")
        RecordingGeminiTranscriber.instances.append(self)


def _gemini_settings(tmp_path, **overrides):
    return Settings(
        gemini_api_key="k", transcribe_engine="gemini", data_dir=tmp_path,
        voice_relay_max_speakers=20, **overrides,
    )


def test_voice_relay_is_skipped_when_pyannote_relabels_speakers(tmp_path, monkeypatch):
    """有 pyannote 時接力標出的代號最後會被整份重標蓋掉，卻要多花數百次上傳往返。"""
    RecordingGeminiTranscriber.instances.clear()
    monkeypatch.setattr("app.main.GeminiTranscriber", RecordingGeminiTranscriber)
    create_app(_gemini_settings(tmp_path, pyannote_api_key="pk"))
    (transcriber,) = RecordingGeminiTranscriber.instances
    assert transcriber.kwargs["voice_relay_max_speakers"] == 0
    # 講者標籤同理：標得再差也會被重標，不值得為它重跑整段轉錄
    assert transcriber.kwargs["speaker_labels_needed"] is False


def test_voice_relay_unchanged_without_pyannote(tmp_path, monkeypatch):
    RecordingGeminiTranscriber.instances.clear()
    monkeypatch.setattr("app.main.GeminiTranscriber", RecordingGeminiTranscriber)
    create_app(_gemini_settings(tmp_path, pyannote_api_key=None))
    (transcriber,) = RecordingGeminiTranscriber.instances
    assert transcriber.kwargs["voice_relay_max_speakers"] == 20
    assert transcriber.kwargs["speaker_labels_needed"] is True


def test_health_reports_whether_pyannote_is_active(tmp_path, monkeypatch):
    """部署後從外部確認金鑰有沒有生效：填了卻沒啟用（例如引擎不是 gemini）要看得出來。"""
    monkeypatch.setattr("app.main.GeminiTranscriber", RecordingGeminiTranscriber)
    on = TestClient(create_app(_gemini_settings(tmp_path, pyannote_api_key="pk")))
    assert on.get("/api/health").json()["speaker_diarization"] == "pyannote"
    off = TestClient(create_app(_gemini_settings(tmp_path / "off", pyannote_api_key=None)))
    assert off.get("/api/health").json()["speaker_diarization"] is None


def test_keyword_search_reads_meetings_in_one_pass(tmp_path):
    """50 場會議原本是 51 次讀取（Firestore 按次計費）：先列表、再逐場 get_meeting。"""

    class CountingStore(LocalJsonStore):
        get_calls = 0

        def get_meeting(self, meeting_id, *, user=DEFAULT_USER):
            CountingStore.get_calls += 1
            return super().get_meeting(meeting_id, user=user)

    settings = Settings(gemini_api_key=None, data_dir=tmp_path)
    store = CountingStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(generate=lambda prompt: valid_json()),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(tmp_path / "notifications"),
    )
    client = TestClient(create_app(
        settings, store=store, orchestrator=orchestrator, transcriber=FakeTranscriber()
    ))
    for _ in range(3):
        make_meeting(client)
    CountingStore.get_calls = 0

    hits = client.get("/api/search", params={"q": "prompt"}).json()["hits"]
    assert len({h["meeting_id"] for h in hits}) == 3  # 逐字稿裡的字照樣搜得到
    assert CountingStore.get_calls == 0


# ---- 速率限制 ----

def _limited_client(tmp_path, limits):
    settings = Settings(
        gemini_api_key=None, data_dir=tmp_path, rate_limit_enabled=True, rate_limits=limits
    )
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(generate=lambda prompt: valid_json()),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(tmp_path / "notifications"),
    )
    return TestClient(create_app(
        settings, store=store, orchestrator=orchestrator, transcriber=FakeTranscriber()
    ))


def test_analyze_is_rate_limited_with_retry_after(tmp_path):
    client = _limited_client(tmp_path, "analyze=2/0")
    for _ in range(2):
        assert client.post("/api/meetings", json={"text": "志明下週一交 prompt"}).status_code == 200
    resp = client.post("/api/meetings", json={"text": "志明下週一交 prompt"})
    assert resp.status_code == 429
    assert "每分鐘最多 2 次" in resp.json()["detail"]
    assert int(resp.headers["Retry-After"]) > 0


def test_media_upload_is_rejected_before_the_file_is_saved(tmp_path):
    client = _limited_client(tmp_path, "media=0/1")

    def upload(meeting_date=None):
        return client.post(
            "/api/media",
            files={"file": ("meeting.wav", io.BytesIO(b"RIFF-fake-wav"), "audio/wav")},
            data={"meeting_date": meeting_date} if meeting_date else None,
        )

    assert upload().status_code == 200
    resp = upload()
    assert resp.status_code == 429
    assert "今天的檔案上傳已達上限 1 次" in resp.json()["detail"]
    # 額度用完時連格式錯誤的請求都回 429 而非 400：限制在任何解析與存檔之前就擋下，
    # 被擋的上傳不會寫進暫存磁碟
    assert upload(meeting_date="下週五").status_code == 429


def test_live_start_and_chunk_are_rate_limited(tmp_path):
    client = _limited_client(tmp_path, "live_start=1/0,live_chunk=1/0")
    sid = client.post("/api/live/start").json()["session_id"]
    assert client.post("/api/live/start").status_code == 429
    def chunk():
        return client.post(
            f"/api/live/{sid}/chunk",
            files={"file": ("c.webm", io.BytesIO(b"audio"), "audio/webm")},
            data={"offset": "0"},
        )

    assert chunk().status_code == 200
    assert chunk().status_code == 429


def test_ask_and_translate_are_rate_limited(tmp_path):
    client = _limited_client(tmp_path, "ask=1/0,translate=1/0")
    first_ask = client.post("/api/ask", json={"question": "誰負責 prompt？"})
    assert first_ask.status_code != 429
    assert client.post("/api/ask", json={"question": "誰負責 prompt？"}).status_code == 429
    first_tr = client.post("/api/translate", json={"text": "hi", "target": "zh"})
    assert first_tr.status_code != 429
    assert client.post("/api/translate", json={"text": "hi", "target": "zh"}).status_code == 429


def test_rate_limit_is_off_by_default_for_local_settings(client):
    for _ in range(15):
        assert client.post("/api/meetings", json={"text": "志明下週一交 prompt"}).status_code == 200


def test_health_reports_rate_limit_state(tmp_path):
    assert _limited_client(tmp_path, "").get("/api/health").json()["rate_limit"] is True
