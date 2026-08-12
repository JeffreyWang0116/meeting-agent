"""資料的使用者歸屬（多租戶鋪墊）。

現在整站只有一個使用者（DEFAULT_USER），但未來要做成 app 就一定得有帳號。
最貴的不是「加上過濾」而是「已經寫進去、沒有主人的舊資料」——那要靠猜測遷移。
所以從現在起每筆寫入都蓋上 user，讀取一律照 user 過濾。

這裡釘住的是隔離性本身：兩個使用者不能看到、改到、刪到對方的東西。
LocalJsonStore 與 FirestoreStore 都必須通過同一組測試，介面才真的一致。
"""
from __future__ import annotations

import pytest

from app.stores.base import DEFAULT_USER
from app.stores.local_store import LocalJsonStore
from tests.test_firestore_store import FakeFirestore  # 既有的假 Firestore


@pytest.fixture(params=["local", "firestore"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalJsonStore(tmp_path / "db.json")
    from app.stores.firestore_store import FirestoreStore

    return FirestoreStore(FakeFirestore())


def _analysis(title: str):
    from app.models import MeetingAnalysis

    return MeetingAnalysis.model_validate({
        "meeting": {"title": title, "date": "2026-08-10", "attendees": []},
        "decisions": [],
        "todos": [{"task": f"{title} 的任務", "priority": "medium"}],
        "pending_items": [],
    })


def test_meetings_are_scoped_to_their_owner(store):
    a = store.save_meeting(_analysis("A 的會議"), user="userA")
    b = store.save_meeting(_analysis("B 的會議"), user="userB")

    assert [m["id"] for m in store.list_meetings(user="userA")] == [a]
    assert [m["id"] for m in store.list_meetings(user="userB")] == [b]
    # 直接用 id 也拿不到別人的
    assert store.get_meeting(a, user="userB") is None
    assert store.get_meeting(a, user="userA")["id"] == a


def test_tasks_follow_the_meeting_owner(store):
    store.save_meeting(_analysis("A 的會議"), user="userA")
    store.save_meeting(_analysis("B 的會議"), user="userB")

    a_tasks = store.list_tasks(user="userA")
    b_tasks = store.list_tasks(user="userB")
    assert len(a_tasks) == 1 and len(b_tasks) == 1
    assert a_tasks[0]["task"].startswith("A")
    assert b_tasks[0]["task"].startswith("B")


def test_cannot_update_or_delete_another_users_data(store):
    a = store.save_meeting(_analysis("A 的會議"), user="userA")
    task_id = store.list_tasks(user="userA")[0]["id"]

    assert store.update_meeting(a, {"kind": "面試"}, user="userB") is None
    assert store.delete_meeting(a, user="userB") is False
    assert store.update_task(task_id, user="userB", status="done") is None
    assert store.delete_task(task_id, user="userB") is False
    # A 自己來就可以
    assert store.update_meeting(a, {"kind": "面試"}, user="userA")["kind"] == "面試"
    assert store.update_task(task_id, user="userA", status="done")["status"] == "done"


def test_glossary_and_roster_are_per_user(store):
    store.save_glossary([{"term": "TaskHub", "note": ""}], user="userA")
    store.save_speaker_roster(["王霖翔"], user="userA")

    assert store.get_glossary(user="userA") == [{"term": "TaskHub", "note": ""}]
    assert store.get_glossary(user="userB") == []
    assert store.get_speaker_roster(user="userA") == ["王霖翔"]
    assert store.get_speaker_roster(user="userB") == []


def test_records_written_without_a_user_belong_to_the_default(store):
    """呼叫端沒指定就是預設使用者——改版前存下來的舊資料也視為它的，
    這樣既有資料不用遷移就仍然讀得到。"""
    mid = store.save_meeting(_analysis("沒指定"))
    assert store.get_meeting(mid, user=DEFAULT_USER)["id"] == mid
    assert [m["id"] for m in store.list_meetings()] == [mid]


def test_legacy_records_without_a_user_field_are_still_visible(tmp_path):
    """真正的舊資料檔沒有 user 欄位，不能因為加了過濾就整批消失。"""
    import json

    path = tmp_path / "db.json"
    path.write_text(json.dumps({
        "meetings": [{"id": "old1", "meeting": {"title": "舊會議", "date": "2026-01-01"}}],
        "tasks": [{"id": "t1", "meeting_id": "old1", "task": "舊任務", "status": "todo"}],
    }, ensure_ascii=False), encoding="utf-8")

    store = LocalJsonStore(path)
    assert [m["id"] for m in store.list_meetings()] == ["old1"]
    assert [t["id"] for t in store.list_tasks()] == ["t1"]
    assert store.get_meeting("old1") is not None


def test_export_and_import_stay_within_one_user(store):
    store.save_meeting(_analysis("A 的會議"), user="userA")
    store.save_meeting(_analysis("B 的會議"), user="userB")

    dump = store.export_all(user="userA")
    assert len(dump["meetings"]) == 1
    assert dump["meetings"][0]["meeting"]["title"] == "A 的會議"

    # 還原只覆蓋自己的資料，不能把別人的清掉
    store.import_all({"meetings": [], "tasks": []}, user="userA")
    assert store.list_meetings(user="userA") == []
    assert len(store.list_meetings(user="userB")) == 1


# ---- 端到端：確認 API 寫進去的資料真的帶著使用者 ----

def test_data_created_through_the_api_is_stamped_with_the_user(tmp_path):
    """store 層有隔離不代表端點有把使用者帶下去。這裡走真實的分析流程，
    直接檢查落地的紀錄有沒有 user 欄位——沒有的話，未來接上登入就會出現
    一批沒有主人的資料。"""
    from fastapi.testclient import TestClient

    from app.agents.decision_agent import DecisionAgent
    from app.agents.executor_agent import ExecutorAgent
    from app.agents.notifier_agent import NotifierAgent
    from app.agents.parser_agent import ParserAgent
    from app.config import Settings
    from app.main import create_app
    from app.orchestrator import Orchestrator
    from tests.test_decision import valid_json

    store = LocalJsonStore(tmp_path / "db.json")
    app = create_app(
        Settings(gemini_api_key=None, data_dir=tmp_path),
        store=store,
        orchestrator=Orchestrator(
            parser=ParserAgent(),
            decision=DecisionAgent(generate=lambda p: valid_json()),
            executor=ExecutorAgent(store),
            notifier=NotifierAgent(tmp_path / "notifications"),
        ),
    )
    client = TestClient(app)
    client.post("/api/meetings", json={"text": "開會內容"})
    client.post("/api/tasks", json={"task": "手動加的任務"})
    client.put("/api/glossary", json={"terms": [{"term": "TaskHub", "note": ""}]})

    raw = (tmp_path / "db.json").read_text(encoding="utf-8")
    import json as _json

    data = _json.loads(raw)
    assert data["meetings"] and all(m["user"] == DEFAULT_USER for m in data["meetings"])
    assert data["tasks"] and all(t["user"] == DEFAULT_USER for t in data["tasks"])
    # 詞彙表改用 by_user 格式儲存，不再是頂層的 terms
    gloss = _json.loads((tmp_path / "glossary.json").read_text(encoding="utf-8"))
    assert gloss["by_user"][DEFAULT_USER][0]["term"] == "TaskHub"


def test_legacy_glossary_and_roster_files_are_still_readable(tmp_path):
    """線上那台的 glossary.json / speakers.json 是舊格式（頂層 terms/names）。
    加了 by_user 之後不能讓那些詞彙整批消失——它們屬於 DEFAULT_USER。"""
    import json

    (tmp_path / "glossary.json").write_text(
        json.dumps({"terms": [{"term": "王霖翔", "note": "人名"}]}, ensure_ascii=False),
        encoding="utf-8")
    (tmp_path / "speakers.json").write_text(
        json.dumps({"names": ["王霖翔", "Kevin"]}, ensure_ascii=False), encoding="utf-8")

    store = LocalJsonStore(tmp_path / "db.json")
    assert store.get_glossary() == [{"term": "王霖翔", "note": "人名"}]
    assert store.get_speaker_roster() == ["王霖翔", "Kevin"]

    # 第一次寫入就把舊格式搬進 by_user，不留兩種格式並存
    store.save_glossary([{"term": "TaskHub", "note": ""}])
    doc = json.loads((tmp_path / "glossary.json").read_text(encoding="utf-8"))
    assert "terms" not in doc
    assert doc["by_user"][DEFAULT_USER] == [{"term": "TaskHub", "note": ""}]
    assert store.get_glossary() == [{"term": "TaskHub", "note": ""}]
