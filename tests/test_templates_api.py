"""自訂範本的 API，以及「用自訂種類分析」真的會把區塊定義餵進提示詞。"""
from __future__ import annotations

import io

from fastapi.testclient import TestClient

from app.agents.decision_agent import DecisionAgent
from app.config import Settings
from app.main import create_app
from app.orchestrator import Orchestrator
from app.stores.local_store import LocalJsonStore
from tests.test_api import FakeTranscriber, wait_for_job


def reply(sections="[]"):
    return (
        '{"meeting": {"title": "專題會議", "date": "2026-10-20", "summary": "s",'
        ' "attendees": []}, "decisions": [], "todos": [], "pending_items": [],'
        f' "highlights": [], "sections": {sections}, "tags": []}}'
    )


def make(tmp_path):
    """記下送進模型的提示詞，回 client.reply 的內容（預設是固定的合法 JSON）。"""
    seen = []

    def generate(prompt):
        seen.append(prompt)
        return client.reply

    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        decision=DecisionAgent(generate=generate),
        store=store,
    )
    client = TestClient(create_app(
        Settings(gemini_api_key=None, data_dir=tmp_path), store=store, orchestrator=orchestrator,
        transcriber=FakeTranscriber(),
    ))
    client.reply = reply()
    client.prompts = seen
    client.store = store
    return client


BODY = {
    "name": "專題 meeting",
    "hint": "學校專題的每週進度會議。",
    "sections": [{"label": "本週進度", "desc": "這週做完的事"},
                 {"label": "卡住的地方", "desc": "遇到的問題"}],
    "features": ["summary", "todos"],
}


def test_create_list_update_delete(tmp_path):
    client = make(tmp_path)

    created = client.post("/api/templates", json=BODY).json()["template"]
    assert created["name"] == "專題 meeting"

    assert len(client.get("/api/templates").json()["templates"]) == 1

    client.patch(f"/api/templates/{created['id']}", json={**BODY, "name": "改過的名稱"})
    assert client.get("/api/templates").json()["templates"][0]["name"] == "改過的名稱"

    client.delete(f"/api/templates/{created['id']}")
    assert client.get("/api/templates").json()["templates"] == []


def test_invalid_template_is_rejected(tmp_path):
    client = make(tmp_path)

    assert client.post("/api/templates", json={"name": "  "}).status_code == 400
    assert client.post("/api/templates", json={
        "name": "x", "sections": [{"label": "A"}, {"label": "A"}]}).status_code == 400


def test_custom_kind_appears_in_the_kind_list(tmp_path):
    client = make(tmp_path)
    created = client.post("/api/templates", json=BODY).json()["template"]

    groups = client.get("/api/meeting-kinds").json()["groups"]
    mine = [g for g in groups if g["label"] == "我的範本"]

    assert mine and mine[0]["kinds"][0]["value"] == f"custom:{created['id']}"
    assert mine[0]["kinds"][0]["features"] == ["summary", "todos"]


def test_analysing_with_a_custom_kind_feeds_the_sections_into_the_prompt(tmp_path):
    client = make(tmp_path)
    created = client.post("/api/templates", json=BODY).json()["template"]

    resp = client.post("/api/meetings", json={
        "text": "本週把登入做完了", "kind": f"custom:{created['id']}"})

    assert resp.status_code == 200
    prompt = client.prompts[-1]
    assert "本週進度" in prompt and "卡住的地方" in prompt
    assert "學校專題的每週進度會議" in prompt
    # features 只有 summary 與 todos → 其餘欄位要叫模型不要輸出
    assert "decisions" in prompt


def test_custom_kind_sections_from_the_model_are_kept(tmp_path):
    """模型照範本回了區塊，不能被內建種類的白名單濾掉。"""
    client = make(tmp_path)
    created = client.post("/api/templates", json=BODY).json()["template"]
    client.reply = reply('[{"label": "本週進度", "items": ["登入做完了"]}, {"label": "亂加的", "items": ["x"]}]')

    mid = client.post("/api/meetings", json={
        "text": "本週把登入做完了", "kind": f"custom:{created['id']}"}).json()["meeting_id"]

    assert client.get(f"/api/meetings/{mid}").json()["sections"] == [
        {"label": "本週進度", "items": ["登入做完了"]}]


def test_uploading_a_file_with_a_custom_kind_feeds_the_sections_into_the_prompt(tmp_path):
    """上傳走背景執行緒，沒有請求上下文：範本要在送出當下就查好帶進去。"""
    client = make(tmp_path)
    created = client.post("/api/templates", json=BODY).json()["template"]

    resp = client.post(
        "/api/media",
        files={"file": ("meeting.wav", io.BytesIO(b"RIFF-fake-wav"), "audio/wav")},
        data={"kind": f"custom:{created['id']}"},
    )
    job = wait_for_job(client, resp.json()["job_id"])

    assert job["status"] == "done"
    prompt = client.prompts[-1]
    assert "本週進度" in prompt and "卡住的地方" in prompt
    assert f"custom:{created['id']}" not in prompt


def test_reanalysing_a_custom_kind_meeting_uses_the_template_and_updates_sections(tmp_path):
    client = make(tmp_path)
    created = client.post("/api/templates", json=BODY).json()["template"]
    mid = client.post("/api/meetings", json={
        "text": "本週把登入做完了", "kind": f"custom:{created['id']}"}).json()["meeting_id"]
    client.reply = reply('[{"label": "本週進度", "items": ["登入做完了"]}]')

    assert client.post(f"/api/meetings/{mid}/reanalyze").status_code == 200

    prompt = client.prompts[-1]
    assert "本週進度" in prompt and "學校專題的每週進度會議" in prompt
    assert client.get(f"/api/meetings/{mid}").json()["sections"] == [
        {"label": "本週進度", "items": ["登入做完了"]}]


def test_the_meeting_remembers_its_custom_kind(tmp_path):
    client = make(tmp_path)
    created = client.post("/api/templates", json=BODY).json()["template"]
    mid = client.post("/api/meetings", json={
        "text": "本週把登入做完了", "kind": f"custom:{created['id']}"}).json()["meeting_id"]

    assert client.get(f"/api/meetings/{mid}").json()["kind"] == f"custom:{created['id']}"


def test_a_meeting_whose_template_was_deleted_still_opens(tmp_path):
    """範本刪掉之後，用它分析過的舊會議不該變成打不開或重新分析失敗。"""
    client = make(tmp_path)
    created = client.post("/api/templates", json=BODY).json()["template"]
    mid = client.post("/api/meetings", json={
        "text": "本週把登入做完了", "kind": f"custom:{created['id']}"}).json()["meeting_id"]
    client.delete(f"/api/templates/{created['id']}")

    assert client.get(f"/api/meetings/{mid}").status_code == 200
    assert client.post(f"/api/meetings/{mid}/reanalyze").status_code == 200


def test_unknown_custom_kind_is_rejected_on_new_analysis(tmp_path):
    client = make(tmp_path)

    resp = client.post("/api/meetings", json={"text": "x", "kind": "custom:nope"})

    assert resp.status_code == 400


def test_editing_a_meeting_whose_template_was_deleted_is_allowed(tmp_path):
    """前端送編輯時會把目前的 kind 一起帶回來。範本刪了就擋下來的話，
    那場會議連標題都改不了。"""
    client = make(tmp_path)
    created = client.post("/api/templates", json=BODY).json()["template"]
    kind = f"custom:{created['id']}"
    mid = client.post("/api/meetings", json={"text": "本週把登入做完了", "kind": kind}).json()["meeting_id"]
    client.delete(f"/api/templates/{created['id']}")

    resp = client.patch(f"/api/meetings/{mid}", json={"title": "改過的標題", "kind": kind})

    assert resp.status_code == 200
    assert client.get(f"/api/meetings/{mid}").json()["meeting"]["title"] == "改過的標題"


def test_switching_a_meeting_to_a_template_that_never_existed_is_rejected(tmp_path):
    client = make(tmp_path)
    mid = client.post("/api/meetings", json={"text": "x"}).json()["meeting_id"]

    assert client.patch(f"/api/meetings/{mid}", json={"kind": "custom:nope"}).status_code == 400
