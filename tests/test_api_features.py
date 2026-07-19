"""功能勾選（會議摘要／決議事項／代辦事項）串接三種輸入路徑的整合測試。

非「會議」種類預設不使用這三個功能；「會議」種類（或未指定種類）預設全部使用，
使用者也可以用 features 明確指定要哪些——不管哪種情況，被停用的功能不只是
畫面不顯示，代辦事項也不會真的寫進任務庫。
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
        # 假 LLM 永遠回傳「全部欄位都有值」的完整 payload：驗證的重點是
        # features 停用的欄位有沒有被後端強制清空，而不是 prompt 內容本身
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


# ---- 文字貼上：/api/meetings ----

def test_meeting_kind_defaults_to_all_features(client):
    resp = client.post("/api/meetings", json={"text": "開會內容", "kind": "會議"})
    a = resp.json()["analysis"]
    assert a["meeting"]["summary"]
    assert a["decisions"]
    assert a["todos"]
    assert len(client.get("/api/tasks").json()["tasks"]) == 1


def test_no_kind_defaults_to_all_features_backward_compat(client):
    """沒指定種類＝維持改動前的既有行為（向後相容）。"""
    resp = client.post("/api/meetings", json={"text": "開會內容"})
    a = resp.json()["analysis"]
    assert a["meeting"]["summary"]
    assert a["todos"]


def test_non_meeting_kind_defaults_to_no_features(client):
    resp = client.post("/api/meetings", json={"text": "打給客戶討論報價", "kind": "通話"})
    a = resp.json()["analysis"]
    assert a["meeting"]["summary"] is None
    assert a["decisions"] == []
    assert a["todos"] == []
    # 最重要的：任務庫真的沒被寫入任務，不只是畫面不顯示
    assert client.get("/api/tasks").json()["tasks"] == []


def test_explicit_features_override_kind_default(client):
    resp = client.post(
        "/api/meetings",
        json={"text": "訪談內容", "kind": "訪談", "features": ["summary"]},
    )
    a = resp.json()["analysis"]
    assert a["meeting"]["summary"]
    assert a["decisions"] == []
    assert a["todos"] == []
    assert client.get("/api/tasks").json()["tasks"] == []


def test_explicit_empty_features_disables_everything_even_for_meeting_kind(client):
    resp = client.post(
        "/api/meetings",
        json={"text": "開會內容", "kind": "會議", "features": []},
    )
    a = resp.json()["analysis"]
    assert a["meeting"]["summary"] is None
    assert a["todos"] == []


def test_unknown_feature_rejected(client):
    resp = client.post(
        "/api/meetings", json={"text": "x", "kind": "會議", "features": ["not_a_feature"]}
    )
    assert resp.status_code == 400


# ---- 檔案上傳：/api/media ----

def test_media_upload_respects_features(client):
    resp = client.post(
        "/api/media",
        files={"file": ("call.wav", io.BytesIO(b"RIFF-fake-wav"), "audio/wav")},
        data={"kind": "通話"},
    )
    job = wait_for_job(client, resp.json()["job_id"])
    a = job["result"]["analysis"]
    assert a["meeting"]["summary"] is None
    assert a["todos"] == []
    assert client.get("/api/tasks").json()["tasks"] == []


def test_media_upload_explicit_features_comma_separated(client):
    resp = client.post(
        "/api/media",
        files={"file": ("m.wav", io.BytesIO(b"RIFF-fake-wav"), "audio/wav")},
        data={"kind": "其它", "features": "decisions,todos"},
    )
    job = wait_for_job(client, resp.json()["job_id"])
    a = job["result"]["analysis"]
    assert a["meeting"]["summary"] is None
    assert a["decisions"]
    assert a["todos"]
    assert len(client.get("/api/tasks").json()["tasks"]) == 1


# ---- 即時聆聽：/api/live/{id}/finish ----

def test_live_finish_respects_features(client):
    sid = client.post("/api/live/start").json()["session_id"]
    client.post(
        f"/api/live/{sid}/chunk",
        files={"file": ("c.webm", io.BytesIO(b"fake"), "audio/webm")},
    )
    resp = client.post(f"/api/live/{sid}/finish", json={"kind": "語音備忘錄"})
    a = resp.json()["analysis"]
    assert a["meeting"]["summary"] is None
    assert a["todos"] == []
    assert client.get("/api/tasks").json()["tasks"] == []


# ---- 重新分析：/api/meetings/{id}/reanalyze ----

def test_reanalyze_defaults_features_from_stored_kind(client):
    meeting_id = client.post(
        "/api/meetings", json={"text": "客戶通話內容", "kind": "通話"}
    ).json()["meeting_id"]
    assert client.get("/api/tasks").json()["tasks"] == []

    resp = client.post(f"/api/meetings/{meeting_id}/reanalyze")
    assert resp.status_code == 200
    a = resp.json()["analysis"]
    assert a["meeting"]["summary"] is None
    assert a["todos"] == []
    assert client.get("/api/tasks").json()["tasks"] == []
