"""請求層的使用者歸屬：背景轉錄工作與即時聆聽 session。

store 那一層早就照 user 隔離了，但「進行中的工作」不在 store 裡——它們活在
記憶體的 MediaJobManager / LiveSessionManager 中，而這兩個原本完全沒有 user
概念：知道 job_id 就看得到別人的逐字稿，知道 session_id 就能往別人的聆聽裡
灌音訊。接上真正的登入之後，這是資料庫隔離擋不到的那一段。

上傳路徑還有另一個陷阱：轉錄跑在背景執行緒，沒有請求上下文，所以 user 必須
在 submit 當下就捕捉好帶進去——原本沒帶，等於不管誰上傳都存成 DEFAULT_USER。
"""
from __future__ import annotations

import io
import time

import pytest
from fastapi.testclient import TestClient

from app.agents.decision_agent import DecisionAgent
from app.agents.executor_agent import ExecutorAgent
from app.agents.notifier_agent import NotifierAgent
from app.agents.parser_agent import ParserAgent
from app.config import Settings
from app.jobs import MediaJobManager
from app.main import create_app
from app.orchestrator import Orchestrator
from app.stores.local_store import LocalJsonStore
from app.transcription.live_session import LiveSessionManager, SessionNotFound
from tests.test_decision import valid_json


class FakeTranscriber:
    device = "cpu"
    model_size = "fake"

    def transcribe(self, path, on_progress=None):
        return "Kevin 說週五要 demo。"


# ---- MediaJobManager ----

def test_job_visible_only_to_its_owner(tmp_path):
    orch = Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(generate=lambda p: valid_json()),
        executor=ExecutorAgent(LocalJsonStore(tmp_path / "db.json")),
        notifier=NotifierAgent(tmp_path / "n"),
    )
    mgr = MediaJobManager(FakeTranscriber(), orch, tmp_path)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF-fake")

    job_id = mgr.submit(audio, user="alice")
    mgr.wait(job_id, timeout=5)

    assert mgr.get(job_id, user="alice") is not None
    assert mgr.get(job_id, user="bob") is None  # 逐字稿不能被別人輪詢到


# ---- LiveSessionManager ----

class SeqTranscriber:
    def __init__(self, texts):
        self.texts = list(texts)

    def transcribe(self, path, on_progress=None):
        return self.texts.pop(0)


def test_live_session_only_reachable_by_its_owner(tmp_path):
    mgr = LiveSessionManager(SeqTranscriber(["大家好"]), tmp_path)
    sid = mgr.start(user="alice")

    with pytest.raises(SessionNotFound):
        mgr.add_chunk(sid, b"x", user="bob")  # 不能往別人的聆聽灌音訊
    with pytest.raises(SessionNotFound):
        mgr.transcript(sid, user="bob")
    with pytest.raises(SessionNotFound):
        mgr.finish(sid, user="bob")

    assert mgr.add_chunk(sid, b"x", user="alice")["text"] == "大家好"
    assert mgr.finish(sid, user="alice") == "大家好"


# ---- 端點：user 必須從請求一路傳到底 ----

@pytest.fixture
def app_as(tmp_path, monkeypatch):
    """讓測試指定「這個請求是誰發的」，模擬登入接上之後的情況。"""
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(generate=lambda p: valid_json()),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(tmp_path / "notifications"),
    )
    client = TestClient(create_app(
        Settings(gemini_api_key=None, data_dir=tmp_path),
        store=store,
        orchestrator=orchestrator,
        transcriber=FakeTranscriber(),
    ))

    def as_user(name):
        monkeypatch.setattr("app.main.current_user", lambda *a, **k: name)
        return client

    return as_user, store


def wait_for_job(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/media/{job_id}").json()
        if job.get("status") in ("done", "error"):
            return job
        time.sleep(0.05)
    raise TimeoutError("媒體工作逾時未完成")


def test_uploaded_meeting_belongs_to_the_uploader(app_as):
    as_user, store = app_as
    alice = as_user("alice")

    job_id = alice.post(
        "/api/media", files={"file": ("m.wav", io.BytesIO(b"RIFF-fake"), "audio/wav")}
    ).json()["job_id"]
    job = wait_for_job(alice, job_id)

    assert job["status"] == "done"
    # 轉錄在背景執行緒跑，沒有請求上下文——user 必須在 submit 當下就捕捉好
    assert [m["id"] for m in store.list_meetings(user="alice")] == [
        job["result"]["meeting_id"]
    ]
    assert store.list_meetings(user="bob") == []


def test_another_user_cannot_poll_someone_elses_job(app_as):
    as_user, _ = app_as
    alice = as_user("alice")
    job_id = alice.post(
        "/api/media", files={"file": ("m.wav", io.BytesIO(b"RIFF-fake"), "audio/wav")}
    ).json()["job_id"]
    wait_for_job(alice, job_id)

    bob = as_user("bob")
    assert bob.get(f"/api/media/{job_id}").status_code == 404


def test_another_user_cannot_push_into_someone_elses_live_session(app_as):
    as_user, _ = app_as
    sid = as_user("alice").post("/api/live/start").json()["session_id"]

    bob = as_user("bob")
    resp = bob.post(
        f"/api/live/{sid}/chunk",
        files={"file": ("c.webm", io.BytesIO(b"x"), "audio/webm")},
    )
    assert resp.status_code == 404
    assert bob.post(f"/api/live/{sid}/finish").status_code == 404
