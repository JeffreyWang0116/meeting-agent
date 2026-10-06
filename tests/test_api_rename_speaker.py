"""POST /api/meetings/{id}/rename-speaker：一次把會議與任務裡的講者代號換成真名。"""
import json

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
from tests.test_models import make_valid_payload


def coded_payload() -> str:
    p = make_valid_payload()
    p["meeting"].update(summary="講者A質疑預算", attendees=["講者A", "講者B"])
    p["decisions"] = [{"description": "講者A 下週提案", "context": "講者B 同意"}]
    p["todos"] = [
        {"task": "講者A 提供報價單", "owner": "講者A", "priority": "high",
         "priority_reason": "講者B 催講者A", "source_quote": "講者A：我來"},
        {"task": "整理紀錄", "owner": "講者B", "priority": "low"},
    ]
    p["pending_items"] = [{"topic": "講者A 的預算數字", "reason": "講者A 要回去查"}]
    p["highlights"] = [{"text": "講者A質疑預算", "time": "0:01", "source_quote": "講者A：預算太少"}]
    return json.dumps(p, ensure_ascii=False)


@pytest.fixture
def client(tmp_path):
    settings = Settings(gemini_api_key=None, data_dir=tmp_path)
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(generate=lambda prompt: coded_payload()),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(tmp_path / "notifications"),
    )
    return TestClient(create_app(settings, store=store, orchestrator=orchestrator))


def make_meeting(client) -> str:
    text = "[0:01] 講者A：預算太少\n[0:05] 講者B：剛剛講者A說的"
    return client.post("/api/meetings", json={"text": text}).json()["meeting_id"]


def rename(client, meeting_id, old, new):
    return client.post(f"/api/meetings/{meeting_id}/rename-speaker", json={"old": old, "new": new})


def test_rename_updates_meeting_texts_and_tasks_in_one_call(client):
    mid = make_meeting(client)
    resp = rename(client, mid, "講者A", "翁曉玲")
    assert resp.status_code == 200
    body = resp.json()

    m = client.get(f"/api/meetings/{mid}").json()
    assert body["meeting"] == m
    assert m["transcript"] == "[0:01] 翁曉玲：預算太少\n[0:05] 講者B：剛剛講者A說的"
    assert m["meeting"]["attendees"] == ["翁曉玲", "講者B"]
    assert m["meeting"]["summary"] == "翁曉玲質疑預算"
    assert m["decisions"][0]["description"] == "翁曉玲 下週提案"
    assert m["pending_items"][0] == {"topic": "翁曉玲 的預算數字", "reason": "翁曉玲 要回去查"}
    assert m["highlights"][0]["text"] == "翁曉玲質疑預算"
    assert m["highlights"][0]["source_quote"] == "講者A：預算太少"  # 引句要能在逐字稿找到

    tasks = client.get("/api/tasks").json()["tasks"]
    assert body["tasks"] == tasks
    t1, t2 = tasks
    assert (t1["task"], t1["owner"], t1["priority_reason"]) == ("翁曉玲 提供報價單", "翁曉玲", "講者B 催翁曉玲")
    assert t1["source_quote"] == "講者A：我來"
    assert t2["owner"] == "講者B"


def test_rename_leaves_other_meetings_tasks_alone(client):
    first, second = make_meeting(client), make_meeting(client)
    rename(client, first, "講者A", "翁曉玲")
    others = [t for t in client.get("/api/tasks").json()["tasks"] if t["meeting_id"] == second]
    assert others[0]["owner"] == "講者A"
    assert others[0]["task"] == "講者A 提供報價單"


def test_rename_trims_new_name(client):
    mid = make_meeting(client)
    rename(client, mid, "講者A", "  翁曉玲 ")
    assert client.get(f"/api/meetings/{mid}").json()["meeting"]["attendees"][0] == "翁曉玲"


def test_rename_rejects_bad_names_and_unknown_meeting(client):
    mid = make_meeting(client)
    assert rename(client, mid, "講者A", "王委員：他說").status_code == 400
    assert rename(client, mid, "講者A", "").status_code == 400
    assert rename(client, mid, "", "翁曉玲").status_code == 400
    assert rename(client, "nope", "講者A", "翁曉玲").status_code == 404
    # 被擋下的請求不能留下半套改動
    assert client.get(f"/api/meetings/{mid}").json()["meeting"]["attendees"] == ["講者A", "講者B"]


def test_rename_returns_a_rebuilt_email_draft(client):
    """確認信草稿是分析當下產的，不重產的話出席者、負責人還是「講者A」。"""
    mid = make_meeting(client)
    draft = rename(client, mid, "講者A", "翁曉玲").json()["email_draft"]
    attendees = draft.split("■ 出席者\n")[1].split("\n")[0]
    assert attendees == "翁曉玲、講者B"
    assert "翁曉玲 提供報價單｜負責人：翁曉玲" in draft
    assert "翁曉玲質疑預算" in draft
    assert "講者A" not in draft


def test_rebuilt_draft_reflects_task_library_edits(client):
    """改名前在任務庫改過期限、刪過任務，草稿要跟著現況，而不是分析當下的版本。"""
    mid = make_meeting(client)
    t1, t2 = client.get("/api/tasks").json()["tasks"]
    client.patch(f"/api/tasks/{t1['id']}", json={"due_date": ""})  # 期限清空存的是空字串
    client.delete(f"/api/tasks/{t2['id']}")
    draft = rename(client, mid, "講者A", "翁曉玲").json()["email_draft"]
    assert "翁曉玲 提供報價單｜負責人：翁曉玲｜期限：未定" in draft
    assert "整理紀錄" not in draft
