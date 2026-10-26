"""POST /api/meetings/{id}/refine：先預覽、再套用。

兩段式是刻意的：AI 默默改掉摘要或負責人，使用者不會發現——這和當初移除
「讓模型猜講者姓名」是同一個理由。預覽階段不寫資料庫，套用階段不打 AI。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.agents.decision_agent import DecisionAgent
from app.config import Settings
from app.main import create_app
from app.orchestrator import Orchestrator
from app.stores.local_store import LocalJsonStore
from tests.test_decision import valid_json


class FakeRefiner:
    """假的修改模組：記下看過的提示詞，回傳預先排好的答案。"""

    def __init__(self, replies=None):
        self.prompts = []
        self.replies = list(replies or ['{"meeting": {"summary": "短摘要"}}'])

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def make(tmp_path, refiner=None):
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        decision=DecisionAgent(generate=lambda p: valid_json()),
        store=store,
    )
    refiner = refiner or FakeRefiner()
    app = create_app(
        Settings(gemini_api_key=None, data_dir=tmp_path),
        store=store, orchestrator=orchestrator, refine_generate=refiner,
    )
    client = TestClient(app)
    client.store = store
    client.refiner = refiner
    return client


def make_meeting(client):
    return client.post("/api/meetings", json={"text": "志明下週一交 prompt"}).json()["meeting_id"]


def test_preview_returns_changes_without_saving(tmp_path):
    client = make(tmp_path)
    mid = make_meeting(client)
    before = client.get(f"/api/meetings/{mid}").json()["meeting"]["summary"]

    resp = client.post(f"/api/meetings/{mid}/refine", json={"request": "把摘要改短一點"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["changes"] == [{"what": "摘要", "before": before, "after": "短摘要"}]
    # 還沒按確定，資料庫不該動
    assert client.get(f"/api/meetings/{mid}").json()["meeting"]["summary"] == before


def test_applying_writes_the_change_and_does_not_call_the_ai_again(tmp_path):
    client = make(tmp_path)
    mid = make_meeting(client)
    patch = client.post(f"/api/meetings/{mid}/refine", json={"request": "把摘要改短一點"}).json()["patch"]
    calls = len(client.refiner.prompts)

    resp = client.post(f"/api/meetings/{mid}/refine", json={"patch": patch})

    assert resp.status_code == 200
    assert client.get(f"/api/meetings/{mid}").json()["meeting"]["summary"] == "短摘要"
    assert len(client.refiner.prompts) == calls  # 套用不該再打一次 AI


def test_applying_a_task_change_keeps_the_task_id(tmp_path):
    client = make(tmp_path)
    mid = make_meeting(client)
    task = client.get("/api/tasks").json()["tasks"][0]
    patch = {"tasks": [{"id": task["id"], "owner": "王小明"}]}

    client.post(f"/api/meetings/{mid}/refine", json={"patch": patch})

    after = client.get("/api/tasks").json()["tasks"][0]
    assert after["id"] == task["id"]
    assert after["owner"] == "王小明"


def test_a_request_that_changes_nothing_says_so(tmp_path):
    client = make(tmp_path, FakeRefiner(["{}"]))
    mid = make_meeting(client)

    body = client.post(f"/api/meetings/{mid}/refine", json={"request": "你好"}).json()

    assert body["changes"] == []


def test_the_model_cannot_rewrite_the_transcript(tmp_path):
    client = make(tmp_path, FakeRefiner(['{"transcript": "偽造的"}']))
    mid = make_meeting(client)
    before = client.get(f"/api/meetings/{mid}").json()["transcript"]

    resp = client.post(f"/api/meetings/{mid}/refine", json={"request": "改逐字稿"})

    assert resp.status_code == 400
    assert "逐字稿" in resp.json()["detail"]
    assert client.get(f"/api/meetings/{mid}").json()["transcript"] == before


def test_applying_a_patch_that_touches_the_transcript_is_also_rejected(tmp_path):
    """前端送上來的 patch 同樣不可信——套用那一步沒有 AI，但有使用者。"""
    client = make(tmp_path)
    mid = make_meeting(client)

    resp = client.post(f"/api/meetings/{mid}/refine", json={"patch": {"transcript": "偽造的"}})

    assert resp.status_code == 400


def test_unknown_meeting_is_404(tmp_path):
    client = make(tmp_path)

    assert client.post("/api/meetings/nope/refine", json={"request": "x"}).status_code == 404


def test_request_and_patch_cannot_both_be_missing(tmp_path):
    client = make(tmp_path)
    mid = make_meeting(client)

    assert client.post(f"/api/meetings/{mid}/refine", json={}).status_code == 400


def test_refine_is_rate_limited(tmp_path):
    """它會打 Gemini，沒節流就是另一個燒額度的入口。"""
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        decision=DecisionAgent(generate=lambda p: valid_json()),
        store=store,
    )
    app = create_app(
        Settings(gemini_api_key=None, data_dir=tmp_path,
                 rate_limit_enabled=True, rate_limits="refine=1/0"),
        store=store, orchestrator=orchestrator, refine_generate=FakeRefiner(),
    )
    client = TestClient(app)
    mid = client.post("/api/meetings", json={"text": "志明下週一交 prompt"}).json()["meeting_id"]

    assert client.post(f"/api/meetings/{mid}/refine", json={"request": "改短"}).status_code == 200
    assert client.post(f"/api/meetings/{mid}/refine", json={"request": "改短"}).status_code == 429


def test_applying_is_not_rate_limited(tmp_path):
    """套用不打 AI，不該佔用同一份額度——否則預覽完卻套用不了。"""
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        decision=DecisionAgent(generate=lambda p: valid_json()),
        store=store,
    )
    app = create_app(
        Settings(gemini_api_key=None, data_dir=tmp_path,
                 rate_limit_enabled=True, rate_limits="refine=1/0"),
        store=store, orchestrator=orchestrator, refine_generate=FakeRefiner(),
    )
    client = TestClient(app)
    mid = client.post("/api/meetings", json={"text": "志明下週一交 prompt"}).json()["meeting_id"]
    patch = client.post(f"/api/meetings/{mid}/refine", json={"request": "改短"}).json()["patch"]

    assert client.post(f"/api/meetings/{mid}/refine", json={"patch": patch}).status_code == 200
