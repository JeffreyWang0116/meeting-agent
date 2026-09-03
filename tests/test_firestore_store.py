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


class _FakeQuery:
    """where() 的結果。只支援等值過濾——store 刻意不用 order_by 或第二個
    條件欄位，才不必為了部署去建 Firestore 複合索引。"""

    def __init__(self, col, field_filter):
        self._col = col
        self._filter = field_filter

    def stream(self):
        assert self._filter.op_string == "==", "假件只實作等值過濾"
        return [
            _FakeSnapshot(k, v)
            for k, v in self._col._docs.items()
            if v.get(self._filter.field_path) == self._filter.value
        ]


class _FakeCollection:
    def __init__(self):
        self._docs = {}
        self.scans = 0  # 整份 collection 掃描的次數（＝Firestore 會計費的讀取）

    def document(self, doc_id):
        return _FakeDocRef(self, doc_id)

    def where(self, filter):
        return _FakeQuery(self, filter)

    def stream(self):
        self.scans += 1
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


def test_update_meeting_merges_info_and_top_level_fields():
    db = FakeFirestore()
    store = make_store(db)
    meeting_id = store.save_meeting(make_analysis(), transcript="原逐字稿")

    updated = store.update_meeting(
        meeting_id,
        {"meeting": {"title": "改過的標題"}, "transcript": "改過的逐字稿"},
    )
    assert updated["meeting"]["title"] == "改過的標題"
    assert updated["meeting"]["date"]
    assert updated["transcript"] == "改過的逐字稿"
    assert make_store(db).get_meeting(meeting_id)["meeting"]["title"] == "改過的標題"
    assert store.update_meeting("no-such-id", {"transcript": "x"}) is None


def test_delete_meeting_removes_meeting_and_its_tasks():
    db = FakeFirestore()
    store = make_store(db)
    id1 = store.save_meeting(make_analysis())
    id2 = store.save_meeting(make_analysis())

    assert store.delete_meeting(id1) is True
    assert store.get_meeting(id1) is None
    assert store.list_tasks(meeting_id=id1) == []
    assert store.get_meeting(id2) is not None
    assert len(store.list_tasks(meeting_id=id2)) == 1
    assert store.delete_meeting(id1) is False


def test_replace_tasks_swaps_meeting_tasks_only():
    store = make_store()
    id1 = store.save_meeting(make_analysis())
    id2 = store.save_meeting(make_analysis())
    old_task_ids = {t["id"] for t in store.list_tasks(meeting_id=id1)}

    new_tasks = store.replace_tasks(
        id1, [{"task": "新任務A", "owner": None, "due_date": None, "priority": "low"}]
    )
    assert len(new_tasks) == 1
    assert new_tasks[0]["status"] == "todo"
    assert {t["id"] for t in store.list_tasks(meeting_id=id1)}.isdisjoint(old_task_ids)
    assert len(store.list_tasks(meeting_id=id2)) == 1


def test_glossary_get_and_save_persists():
    db = FakeFirestore()
    store = make_store(db)
    assert store.get_glossary() == []
    store.save_glossary([{"term": "王霖翔", "note": "人名"}])
    assert store.get_glossary() == [{"term": "王霖翔", "note": "人名"}]
    # 換一個 store 連同一個後端（等同重啟）仍在
    assert make_store(db).get_glossary() == [{"term": "王霖翔", "note": "人名"}]


def test_speaker_roster_get_and_save_persists():
    db = FakeFirestore()
    store = make_store(db)
    assert store.get_speaker_roster() == []
    store.save_speaker_roster(["王霖翔", "李經理"])
    assert make_store(db).get_speaker_roster() == ["王霖翔", "李經理"]


def test_add_manual_task():
    db = FakeFirestore()
    store = make_store(db)
    t = store.add_task({"task": "買咖啡", "priority": "high", "meeting_id": None})
    assert t["id"] and t["status"] == "todo" and t["priority"] == "high"
    assert t["meeting_id"] is None
    assert make_store(db).list_tasks()[0]["task"] == "買咖啡"


def test_export_import_roundtrip():
    db = FakeFirestore()
    store = make_store(db)
    store.save_meeting(make_analysis(), transcript="逐字稿原文")
    store.save_glossary([{"term": "TaskHub", "note": ""}])
    store.save_speaker_roster(["王霖翔"])

    dump = store.export_all()
    assert dump["meetings"][0]["transcript"] == "逐字稿原文"
    assert dump["tasks"] and dump["glossary"] and dump["speaker_roster"]

    # 匯入到另一個後端 → 內容一致
    other = make_store(FakeFirestore())
    other.import_all(dump)
    assert other.list_meetings()[0]["meeting"]["title"] == "專題進度會議"
    assert other.list_tasks()[0]["task"] == "完成 Prompt 初版"
    assert other.get_glossary() == [{"term": "TaskHub", "note": ""}]
    assert other.get_speaker_roster() == ["王霖翔"]

    # 整份覆蓋：同一後端匯入空資料會清掉
    store.import_all({"meetings": [], "tasks": []})
    assert make_store(db).list_meetings() == []
    assert make_store(db).list_tasks() == []


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


# ---- 讀取成本：伺服器端過濾，不整份 collection 掃回來 ----
# Firestore 按「讀取的文件數」計費。原本是 stream() 整份撈回來再用 Python
# 過濾，代表自己只有 5 場會議，也要為資料庫裡所有人的 500 場付費。

def test_list_meetings_does_not_scan_the_whole_collection():
    db = FakeFirestore()
    store = make_store(db)
    store.save_meeting(make_analysis(), user="me")
    for _ in range(3):
        store.save_meeting(make_analysis(), user="other")

    store.list_meetings(user="me")  # 第一次會做一次性的舊資料欄位補齊
    db.collection("meetings").scans = 0

    mine = store.list_meetings(user="me")

    assert len(mine) == 1
    assert db.collection("meetings").scans == 0


def test_list_tasks_does_not_scan_the_whole_collection():
    db = FakeFirestore()
    store = make_store(db)
    store.save_meeting(make_analysis(), user="me")
    store.save_meeting(make_analysis(), user="other")

    store.list_tasks(user="me")
    db.collection("tasks").scans = 0

    assert len(store.list_tasks(user="me")) == 1
    assert db.collection("tasks").scans == 0


def test_legacy_documents_without_user_field_are_backfilled():
    """where("user","==",x) 查不到「根本沒有 user 欄位」的文件，所以改用
    伺服器端過濾之前必須先把舊資料補上欄位，否則改版前的會議會整批消失。"""
    db = FakeFirestore()
    db.collection("meetings").document("old").set({
        "id": "old",
        "created_at": "2026-01-01T00:00:00+00:00",
        "meeting": {"title": "改版前的會議"},
    })
    db.collection("tasks").document("oldtask").set({
        "id": "oldtask",
        "meeting_id": "old",
        "created_at": "2026-01-01T00:00:00+00:00",
        "task": "改版前的任務",
    })
    store = make_store(db)

    assert [m["meeting"]["title"] for m in store.list_meetings()] == ["改版前的會議"]
    assert [t["task"] for t in store.list_tasks()] == ["改版前的任務"]
    assert db.collection("meetings")._docs["old"]["user"] == "local"


def test_backfill_runs_once_per_collection():
    db = FakeFirestore()
    store = make_store(db)
    store.save_meeting(make_analysis())

    store.list_meetings()
    scans_after_first = db.collection("meetings").scans
    for _ in range(5):
        store.list_meetings()

    assert db.collection("meetings").scans == scans_after_first


def test_backfill_not_repeated_by_a_new_store_instance():
    """補齊完成的旗標記在 meta 文件裡，重新部署／換 process 也不必再掃一次。"""
    db = FakeFirestore()
    make_store(db).list_meetings()
    scans_after_first = db.collection("meetings").scans

    make_store(db).list_meetings()

    assert db.collection("meetings").scans == scans_after_first
