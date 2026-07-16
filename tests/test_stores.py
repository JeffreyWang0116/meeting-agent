"""LocalJsonStore：本地 JSON 任務庫（Firestore 的暫代實作）測試。"""
import json

from app.models import MeetingAnalysis
from app.stores.local_store import LocalJsonStore
from tests.test_models import make_valid_payload


def make_analysis() -> MeetingAnalysis:
    return MeetingAnalysis.model_validate(make_valid_payload())


def test_save_meeting_returns_id_and_persists(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    meeting_id = store.save_meeting(make_analysis())
    assert meeting_id

    saved = store.get_meeting(meeting_id)
    assert saved is not None
    assert saved["meeting"]["title"] == "專題進度會議"
    assert saved["id"] == meeting_id


def test_tasks_are_flattened_with_meeting_reference(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    meeting_id = store.save_meeting(make_analysis())

    tasks = store.list_tasks()
    assert len(tasks) == 1
    task = tasks[0]
    assert task["meeting_id"] == meeting_id
    assert task["task"] == "完成 Prompt 初版"
    assert task["owner"] == "王鈺翔"
    assert task["id"]


def test_list_tasks_filtered_by_meeting(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    id1 = store.save_meeting(make_analysis())
    id2 = store.save_meeting(make_analysis())

    assert len(store.list_tasks()) == 2
    assert all(t["meeting_id"] == id1 for t in store.list_tasks(meeting_id=id1))
    assert len(store.list_tasks(meeting_id=id2)) == 1


def test_data_survives_reload(tmp_path):
    path = tmp_path / "db.json"
    meeting_id = LocalJsonStore(path).save_meeting(make_analysis())

    reloaded = LocalJsonStore(path)
    assert reloaded.get_meeting(meeting_id) is not None
    assert len(reloaded.list_tasks()) == 1


def test_file_is_readable_utf8_json(tmp_path):
    path = tmp_path / "db.json"
    LocalJsonStore(path).save_meeting(make_analysis())
    data = json.loads(path.read_text(encoding="utf-8"))
    # 中文不應被轉成 \uXXXX，方便人工檢查
    assert "專題進度會議" in path.read_text(encoding="utf-8")
    assert set(data) == {"meetings", "tasks"}


def test_get_missing_meeting_returns_none(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    assert store.get_meeting("no-such-id") is None


def test_new_tasks_default_status_todo(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    store.save_meeting(make_analysis())
    assert store.list_tasks()[0]["status"] == "todo"


def test_update_task_status_and_fields(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    store.save_meeting(make_analysis())
    task_id = store.list_tasks()[0]["id"]

    updated = store.update_task(task_id, status="doing", owner="Kevin")
    assert updated["status"] == "doing"
    assert updated["owner"] == "Kevin"

    # 要真的落地：重載後仍是更新後的值
    reloaded = LocalJsonStore(tmp_path / "db.json")
    assert reloaded.list_tasks()[0]["status"] == "doing"


def test_update_missing_task_returns_none(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    assert store.update_task("no-such-id", status="done") is None


def test_delete_task(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    store.save_meeting(make_analysis())
    task_id = store.list_tasks()[0]["id"]

    assert store.delete_task(task_id) is True
    assert store.list_tasks() == []
    assert store.delete_task(task_id) is False  # 已刪除

    # 刪除要落地
    assert LocalJsonStore(tmp_path / "db.json").list_tasks() == []


def test_old_db_without_status_gets_todo_on_list(tmp_path):
    """部署上已存在的舊資料沒有 status 欄位，讀取時要補預設值。"""
    path = tmp_path / "db.json"
    store = LocalJsonStore(path)
    store.save_meeting(make_analysis())
    # 模擬舊版資料：手動移除 status
    data = json.loads(path.read_text(encoding="utf-8"))
    for t in data["tasks"]:
        t.pop("status", None)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    assert LocalJsonStore(path).list_tasks()[0]["status"] == "todo"


def test_save_meeting_stores_ai_suggested_tags(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    meeting_id = store.save_meeting(make_analysis())
    assert store.get_meeting(meeting_id)["tags"] == ["專題", "進度會議"]
    assert store.list_meetings()[0]["tags"] == ["專題", "進度會議"]


def test_save_meeting_stores_kind(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    meeting_id = store.save_meeting(make_analysis(), kind="講座")
    assert store.get_meeting(meeting_id)["kind"] == "講座"
    assert store.list_meetings()[0]["kind"] == "講座"
    # 沒給種類時為 None（相容舊資料）
    other = store.save_meeting(make_analysis())
    assert store.get_meeting(other)["kind"] is None


def test_update_meeting_merges_info_and_top_level_fields(tmp_path):
    path = tmp_path / "db.json"
    store = LocalJsonStore(path)
    meeting_id = store.save_meeting(make_analysis(), transcript="原逐字稿")

    updated = store.update_meeting(
        meeting_id,
        {"meeting": {"title": "改過的標題"}, "transcript": "改過的逐字稿"},
    )
    assert updated["meeting"]["title"] == "改過的標題"
    assert updated["meeting"]["date"]  # 其餘欄位保留
    assert updated["transcript"] == "改過的逐字稿"

    # 要落地
    reloaded = LocalJsonStore(path).get_meeting(meeting_id)
    assert reloaded["meeting"]["title"] == "改過的標題"
    assert store.update_meeting("no-such-id", {"transcript": "x"}) is None


def test_delete_meeting_removes_meeting_and_its_tasks(tmp_path):
    path = tmp_path / "db.json"
    store = LocalJsonStore(path)
    id1 = store.save_meeting(make_analysis())
    id2 = store.save_meeting(make_analysis())

    assert store.delete_meeting(id1) is True
    assert store.get_meeting(id1) is None
    assert store.list_tasks(meeting_id=id1) == []
    # 其他會議不受影響
    assert store.get_meeting(id2) is not None
    assert len(store.list_tasks(meeting_id=id2)) == 1
    assert store.delete_meeting(id1) is False

    # 要落地
    assert LocalJsonStore(path).get_meeting(id1) is None


def test_replace_tasks_swaps_meeting_tasks_only(tmp_path):
    path = tmp_path / "db.json"
    store = LocalJsonStore(path)
    id1 = store.save_meeting(make_analysis())
    id2 = store.save_meeting(make_analysis())
    old_task_ids = {t["id"] for t in store.list_tasks(meeting_id=id1)}

    new_tasks = store.replace_tasks(
        id1, [{"task": "新任務A", "owner": None, "due_date": None, "priority": "low"}]
    )
    assert len(new_tasks) == 1
    assert new_tasks[0]["task"] == "新任務A"
    assert new_tasks[0]["status"] == "todo"
    assert new_tasks[0]["meeting_id"] == id1
    # 舊任務被換掉、別場會議的任務不受影響
    assert {t["id"] for t in store.list_tasks(meeting_id=id1)}.isdisjoint(old_task_ids)
    assert len(store.list_tasks(meeting_id=id2)) == 1
    # 要落地
    assert LocalJsonStore(path).list_tasks(meeting_id=id1)[0]["task"] == "新任務A"


def test_glossary_get_and_save_persists(tmp_path):
    path = tmp_path / "db.json"
    store = LocalJsonStore(path)
    assert store.get_glossary() == []
    store.save_glossary([{"term": "王霖翔", "note": "人名"}])
    assert store.get_glossary() == [{"term": "王霖翔", "note": "人名"}]
    # 重載後仍在（落地）
    assert LocalJsonStore(path).get_glossary() == [{"term": "王霖翔", "note": "人名"}]


def test_list_meetings_newest_first(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    id1 = store.save_meeting(make_analysis())
    id2 = store.save_meeting(make_analysis())
    meetings = store.list_meetings()
    assert [m["id"] for m in meetings] == [id2, id1]


def test_add_manual_task_normalizes_and_persists(tmp_path):
    path = tmp_path / "db.json"
    store = LocalJsonStore(path)
    t = store.add_task(
        {"task": "買咖啡", "due_date": "2026-08-01", "priority": "high", "meeting_id": None}
    )
    assert t["id"]
    assert t["status"] == "todo"  # 補上預設狀態
    assert t["priority"] == "high"
    assert t["meeting_id"] is None  # 手動任務不綁會議
    assert t["owner"] is None
    # 落地
    assert LocalJsonStore(path).list_tasks()[0]["task"] == "買咖啡"


def test_export_import_roundtrip_and_overwrite(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    store.save_meeting(make_analysis(), transcript="逐字稿原文")
    store.save_glossary([{"term": "TaskHub", "note": ""}])

    dump = store.export_all()
    assert dump["meetings"][0]["transcript"] == "逐字稿原文"  # 備份含逐字稿全文
    assert dump["tasks"] and dump["glossary"]

    # 匯入到全新的 store → 內容一致
    fresh = LocalJsonStore(tmp_path / "db2.json")
    fresh.import_all(dump)
    assert fresh.list_meetings()[0]["meeting"]["title"] == "專題進度會議"
    assert fresh.list_tasks()[0]["task"] == "完成 Prompt 初版"
    assert fresh.get_glossary() == [{"term": "TaskHub", "note": ""}]

    # import 是「整份覆蓋」：匯入空資料會清掉現有內容
    fresh.import_all({"meetings": [], "tasks": []})
    assert fresh.list_meetings() == []
    assert fresh.list_tasks() == []
