"""FirestoreStore 測試：注入「假 Firestore」client，不觸網。

FirestoreStore 與 LocalJsonStore 實作同一個 TaskStore 介面，行為必須一致
（save/get/list/update/delete、狀態預設 todo、逐字稿存起來但列表剔除全文、
會議新到舊排序），差別只在後端是 Firestore 而非本地 JSON 檔。
"""
from app.models import MeetingAnalysis
from app.stores.firestore_store import FirestoreStore
from tests.test_models import make_valid_payload


def make_analysis() -> MeetingAnalysis:
    return MeetingAnalysis.model_validate(make_valid_payload())


# ---- 最小 Firestore 假件（只實作 store 用到的介面） ----

class _FakeSnapshot:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _FakeDocRef:
    def __init__(self, col, doc_id):
        self._col = col
        self.id = doc_id

    def set(self, data):
        self._col._docs[self.id] = dict(data)

    def get(self):
        return _FakeSnapshot(self.id, self._col._docs.get(self.id))

    def update(self, fields):
        if self.id not in self._col._docs:
            raise KeyError(self.id)
        self._col._docs[self.id].update(fields)

    def delete(self):
        self._col._docs.pop(self.id, None)


class _FakeCollection:
    def __init__(self):
        self._docs = {}

    def document(self, doc_id):
        return _FakeDocRef(self, doc_id)

    def stream(self):
        return [_FakeSnapshot(k, v) for k, v in self._docs.items()]


class FakeFirestore:
    def __init__(self):
        self._cols = {}

    def collection(self, name):
        return self._cols.setdefault(name, _FakeCollection())


def make_store(db=None):
    return FirestoreStore(db or FakeFirestore())


# ---- 行為（對齊 test_stores.py） ----

def test_save_meeting_returns_id_and_persists():
    store = make_store()
    meeting_id = store.save_meeting(make_analysis())
    assert meeting_id
    saved = store.get_meeting(meeting_id)
    assert saved is not None
    assert saved["meeting"]["title"] == "專題進度會議"
    assert saved["id"] == meeting_id


def test_tasks_flattened_with_meeting_reference_and_default_status():
    store = make_store()
    meeting_id = store.save_meeting(make_analysis())
    tasks = store.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["meeting_id"] == meeting_id
    assert tasks[0]["task"] == "完成 Prompt 初版"
    assert tasks[0]["owner"] == "王鈺翔"
    assert tasks[0]["id"]
    assert tasks[0]["status"] == "todo"


def test_list_tasks_filtered_by_meeting():
    store = make_store()
    id1 = store.save_meeting(make_analysis())
    id2 = store.save_meeting(make_analysis())
    assert len(store.list_tasks()) == 2
    assert all(t["meeting_id"] == id1 for t in store.list_tasks(meeting_id=id1))
    assert len(store.list_tasks(meeting_id=id2)) == 1


def test_data_survives_new_store_on_same_db():
    db = FakeFirestore()
    meeting_id = make_store(db).save_meeting(make_analysis())
    reloaded = make_store(db)  # 等同重新啟動：連同一個後端
    assert reloaded.get_meeting(meeting_id) is not None
    assert len(reloaded.list_tasks()) == 1


def test_get_missing_meeting_returns_none():
    assert make_store().get_meeting("no-such-id") is None


def test_update_task_status_and_fields_persists():
    db = FakeFirestore()
    store = make_store(db)
    store.save_meeting(make_analysis())
    task_id = store.list_tasks()[0]["id"]

    updated = store.update_task(task_id, status="doing", owner="Kevin")
    assert updated["status"] == "doing"
    assert updated["owner"] == "Kevin"
    assert make_store(db).list_tasks()[0]["status"] == "doing"


def test_update_missing_task_returns_none():
    assert make_store().update_task("no-such-id", status="done") is None


def test_delete_task_persists():
    db = FakeFirestore()
    store = make_store(db)
    store.save_meeting(make_analysis())
    task_id = store.list_tasks()[0]["id"]

    assert store.delete_task(task_id) is True
    assert store.list_tasks() == []
    assert store.delete_task(task_id) is False
    assert make_store(db).list_tasks() == []


def test_list_meetings_newest_first():
    store = make_store()
    id1 = store.save_meeting(make_analysis())
    id2 = store.save_meeting(make_analysis())
    assert [m["id"] for m in store.list_meetings()] == [id2, id1]


def test_transcript_stored_but_stripped_from_list():
    store = make_store()
    meeting_id = store.save_meeting(make_analysis(), transcript="Kevin：API 小明負責。")
    assert store.get_meeting(meeting_id)["transcript"] == "Kevin：API 小明負責。"
    assert "transcript" not in store.list_meetings()[0]


def test_save_meeting_stores_kind():
    store = make_store()
    meeting_id = store.save_meeting(make_analysis(), kind="通話")
    assert store.get_meeting(meeting_id)["kind"] == "通話"
    assert store.list_meetings()[0]["kind"] == "通話"


def test_task_without_status_backfilled_to_todo():
    """舊資料（Firestore 上已存在、沒有 status 欄位）讀取時要補 todo。"""
    db = FakeFirestore()
    store = make_store(db)
    store.save_meeting(make_analysis())
    # 直接動後端資料，模擬舊版寫入的 task 沒有 status
    tasks_col = db.collection("tasks")
    (doc_id,) = list(tasks_col._docs)
    tasks_col._docs[doc_id].pop("status", None)

    assert make_store(db).list_tasks()[0]["status"] == "todo"
