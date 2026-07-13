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


def test_list_meetings_newest_first(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    id1 = store.save_meeting(make_analysis())
    id2 = store.save_meeting(make_analysis())
    meetings = store.list_meetings()
    assert [m["id"] for m in meetings] == [id2, id1]
