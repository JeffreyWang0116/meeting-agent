"""匯出端點：PDF、Word、字幕檔。

檔名要帶會議標題——下載資料夾裡躺著五個 meeting-a1b2c3.pdf 的時候，
使用者根本不知道哪個是哪個。
"""
from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.agents.decision_agent import DecisionAgent
from app.config import Settings
from app.main import create_app
from app.orchestrator import Orchestrator
from app.stores.local_store import LocalJsonStore
from tests.test_decision import valid_json


@pytest.fixture
def client(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(decision=DecisionAgent(generate=lambda p: valid_json()), store=store)
    app = create_app(Settings(gemini_api_key=None, data_dir=tmp_path),
                     store=store, orchestrator=orchestrator)
    c = TestClient(app)
    c.store = store
    return c


@pytest.fixture
def mid(client):
    return client.post("/api/meetings", json={
        "text": "[0:01] 講者A：今天要談預算\n[0:09] 講者B：我覺得維持原案",
    }).json()["meeting_id"]


# ---- Word ----

def test_word_export_is_a_docx(client, mid):
    resp = client.get(f"/api/meetings/{mid}/report.docx")

    assert resp.status_code == 200
    assert "wordprocessingml" in resp.headers["content-type"]
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        assert "word/document.xml" in z.namelist()


# ---- PDF ----

def test_pdf_export_is_a_pdf(client, mid):
    resp = client.get(f"/api/meetings/{mid}/report.pdf")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.content.startswith(b"%PDF-")


# ---- 字幕 ----

def test_srt_export(client, mid):
    resp = client.get(f"/api/meetings/{mid}/transcript.srt")

    assert resp.status_code == 200
    assert "00:00:01,000 --> 00:00:09,000" in resp.text


def test_vtt_export(client, mid):
    resp = client.get(f"/api/meetings/{mid}/transcript.vtt")

    assert resp.text.startswith("WEBVTT")


def test_subtitles_need_timestamps(client):
    """沒有時間戳就做不出字幕，回一個空檔案只會讓人以為匯出壞了。"""
    plain = client.post("/api/meetings", json={"text": "就是一段沒有時間的文字"}).json()["meeting_id"]

    assert client.get(f"/api/meetings/{plain}/transcript.srt").status_code == 422


# ---- 共通 ----

@pytest.mark.parametrize("ext", ["pdf", "docx"])
def test_the_filename_carries_the_meeting_title(client, mid, ext):
    """下載資料夾裡躺著五個 meeting-a1b2c3.pdf 的時候，誰也認不出哪個是哪個。"""
    resp = client.get(f"/api/meetings/{mid}/report.{ext}")

    disposition = resp.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition  # 中文檔名要用 RFC 5987 編碼
    assert disposition.endswith(f".{ext}") or f".{ext}" in disposition


@pytest.mark.parametrize("path", ["report.pdf", "report.docx", "transcript.srt", "transcript.vtt"])
def test_an_unknown_meeting_is_404(client, path):
    assert client.get(f"/api/meetings/nope/{path}").status_code == 404


@pytest.mark.parametrize("path", ["report.pdf", "report.docx", "transcript.srt"])
def test_a_trashed_meeting_cannot_be_exported(client, mid, path):
    client.delete(f"/api/meetings/{mid}")

    assert client.get(f"/api/meetings/{mid}/{path}").status_code == 404
