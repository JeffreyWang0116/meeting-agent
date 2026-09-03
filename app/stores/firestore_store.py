"""Firebase Firestore 實作的 TaskStore（雲端持久化）。

與 LocalJsonStore 行為一致、實作同一介面，可在 create_app 直接互換：
本機開發不填金鑰用 JSON，雲端填了 Firebase 金鑰就換成這個，資料就不會
在 Render 重新部署時被清空。

資料模型：兩個 collection——`meetings`（每場會議一份文件，含逐字稿全文）、
`tasks`（每筆代辦一份文件，帶 meeting_id）。

使用者過濾在 Firestore 端做（where user == …），排序留在 Python：Firestore
是按「讀取的文件數」計費，整份 collection 撈回來再過濾，等於為資料庫裡所有
人的資料付費。只用單一欄位的等值條件、不加 order_by，就不必建複合索引，
維持零設定即可部署。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

from app.models import MeetingAnalysis
from app.stores.base import DEFAULT_USER, TaskStore, owns, scoped_read, scoped_write


def _user_filter(user: str):
    """where 用的等值條件。firestore SDK 的匯入維持延遲（見 stores/__init__），
    本機沒裝 firebase-admin 也能正常走 JSON 分支。"""
    from google.cloud.firestore_v1.base_query import FieldFilter

    return FieldFilter("user", "==", user)


class FirestoreStore(TaskStore):
    backend = "firestore"

    def __init__(
        self,
        db,
        *,
        meetings: str = "meetings",
        tasks: str = "tasks",
        meta: str = "meta",
    ):
        self._db = db
        self._meetings = meetings
        self._tasks = tasks
        self._meta = meta
        self._lock = threading.Lock()
        self._backfill_lock = threading.Lock()  # 與 _lock 分開，才能在鎖內安全呼叫
        self._backfilled = False
        self._last_ts: datetime | None = None

    # ---- 建構：從金鑰初始化真正的 Firestore client ----

    @classmethod
    def from_credentials(cls, *, cred_json: str | None = None, cred_file: str | None = None):
        import json

        import firebase_admin
        from firebase_admin import credentials, firestore

        if cred_file:
            cred = credentials.Certificate(cred_file)
        elif cred_json:
            cred = credentials.Certificate(json.loads(cred_json))
        else:
            raise ValueError("需要 FIREBASE_CREDENTIALS_FILE 或 FIREBASE_CREDENTIALS_JSON")

        try:
            firebase_admin.get_app()  # 一個 process 只能 initialize 一次
        except ValueError:
            firebase_admin.initialize_app(cred)
        return cls(firestore.client())

    # ---- 內部 ----

    def _now(self) -> str:
        """嚴格遞增的時間戳：同一 process 內連續寫入也保證先後可排序
        （避免兩筆相同微秒導致新到舊排序不穩定）。"""
        ts = datetime.now(timezone.utc)
        if self._last_ts is not None and ts <= self._last_ts:
            ts = self._last_ts + timedelta(microseconds=1)
        self._last_ts = ts
        return ts.isoformat()

    # 一次性資料補齊的旗標（存在 meta/migrations）
    _MIGRATION_DOC = "migrations"
    _BACKFILL_FLAG = "user_backfill"

    def _ensure_user_field(self) -> None:
        """把沒有 user 欄位的舊文件補成 DEFAULT_USER。

        where("user","==",x) 查不到「根本沒有這個欄位」的文件——所以改用
        伺服器端過濾的同時一定要補齊舊資料，否則改版前存的會議會整批查不到。
        那比慢更糟：是無聲的資料消失。

        補完在 meta/migrations 記旗標，換 process／重新部署都不再全表掃描；
        每個 process 也只讀一次旗標。
        """
        if self._backfilled:
            return
        with self._backfill_lock:
            if self._backfilled:
                return
            ref = self._db.collection(self._meta).document(self._MIGRATION_DOC)
            snap = ref.get()
            flags = snap.to_dict() if snap.exists else {}
            if not flags.get(self._BACKFILL_FLAG):
                for coll in (self._meetings, self._tasks):
                    for doc in self._db.collection(coll).stream():
                        if not doc.to_dict().get("user"):
                            self._db.collection(coll).document(doc.id).update(
                                {"user": DEFAULT_USER}
                            )
                ref.set({**flags, self._BACKFILL_FLAG: True})
            self._backfilled = True

    def _user_docs(self, collection: str, user: str) -> list[dict]:
        """這個使用者的文件，過濾在 Firestore 端完成（見模組 docstring）。"""
        self._ensure_user_field()
        query = self._db.collection(collection).where(filter=_user_filter(user))
        return [doc.to_dict() for doc in query.stream()]

    # ---- TaskStore 介面 ----

    def save_meeting(
        self,
        analysis: MeetingAnalysis,
        transcript: str | None = None,
        kind: str | None = None,
        terms: list[dict] | None = None,
        user: str = DEFAULT_USER,
    ) -> str:
        meeting_id = uuid.uuid4().hex[:12]
        dumped = analysis.model_dump(mode="json")

        with self._lock:
            self._db.collection(self._meetings).document(meeting_id).set({
                "id": meeting_id,
                "created_at": self._now(),
                "user": user,
                "meeting": dumped["meeting"],
                "decisions": dumped["decisions"],
                "pending_items": dumped["pending_items"],
                "highlights": dumped.get("highlights", []),
                "sections": dumped.get("sections", []),
                "transcript": transcript,
                "kind": kind,
                # 本次專用詞彙：存起來「重新分析」才不會把使用者會前打的詞弄丟
                "terms": terms or [],
                "tags": dumped.get("tags", []),
            })
            for todo in dumped["todos"]:
                task_id = uuid.uuid4().hex[:12]
                self._db.collection(self._tasks).document(task_id).set({
                    "id": task_id,
                    "meeting_id": meeting_id,
                    "created_at": self._now(),
                    "user": user,
                    "status": "todo",
                    **todo,
                })
        return meeting_id

    def get_meeting(self, meeting_id: str, *, user: str = DEFAULT_USER) -> dict | None:
        snap = self._db.collection(self._meetings).document(meeting_id).get()
        if not snap.exists:
            return None
        record = snap.to_dict()
        return record if owns(record, user) else None

    def list_meetings(self, *, user: str = DEFAULT_USER) -> list[dict]:
        docs = self._user_docs(self._meetings, user)
        docs.sort(key=lambda m: m.get("created_at", ""), reverse=True)  # 新到舊
        # 逐字稿可能數十 KB，列表回應剔除全文保持輕量（get_meeting 才回傳）
        return [{k: v for k, v in m.items() if k != "transcript"} for m in docs]

    def update_meeting(self, meeting_id: str, fields: dict, *, user: str = DEFAULT_USER) -> dict | None:
        with self._lock:
            ref = self._db.collection(self._meetings).document(meeting_id)
            snap = ref.get()
            if not snap.exists:
                return None
            merged = snap.to_dict()
            if not owns(merged, user):
                return None
            f = dict(fields)
            nested = f.pop("meeting", None)
            if nested:
                merged.setdefault("meeting", {}).update(nested)
            merged.update(f)
            ref.set(merged)
            return merged

    def delete_meeting(self, meeting_id: str, *, user: str = DEFAULT_USER) -> bool:
        with self._lock:
            ref = self._db.collection(self._meetings).document(meeting_id)
            snap = ref.get()
            if not snap.exists or not owns(snap.to_dict(), user):
                return False
            ref.delete()
            for task in self._user_docs(self._tasks, user):
                if task.get("meeting_id") == meeting_id:
                    self._db.collection(self._tasks).document(task["id"]).delete()
            return True

    def list_tasks(self, meeting_id: str | None = None, *, user: str = DEFAULT_USER) -> list[dict]:
        docs = self._user_docs(self._tasks, user)
        if meeting_id is not None:
            docs = [t for t in docs if t.get("meeting_id") == meeting_id]
        docs.sort(key=lambda t: t.get("created_at", ""))  # 建立順序
        for t in docs:  # 舊資料沒有 status 欄位，補預設值
            t.setdefault("status", "todo")
        return docs

    def add_task(self, task: dict, *, user: str = DEFAULT_USER) -> dict:
        task_id = uuid.uuid4().hex[:12]
        record = {
            "id": task_id,
            "meeting_id": task.get("meeting_id"),
            "created_at": self._now(),
            "user": user,
            "status": task.get("status") or "todo",
            "priority": task.get("priority") or "medium",
        }
        for field_name in ("task", "owner", "due_date", "source_quote"):
            record[field_name] = task.get(field_name)
        with self._lock:
            self._db.collection(self._tasks).document(task_id).set(record)
        return record

    def update_task(self, task_id: str, *, user: str = DEFAULT_USER, **fields) -> dict | None:
        with self._lock:
            ref = self._db.collection(self._tasks).document(task_id)
            snap = ref.get()
            if not snap.exists or not owns(snap.to_dict(), user):
                return None
            ref.update(fields)
            merged = snap.to_dict()
            merged.update(fields)
            merged.setdefault("status", "todo")
            return merged

    def replace_tasks(self, meeting_id: str, todos: list[dict], *, user: str = DEFAULT_USER) -> list[dict]:
        with self._lock:
            for task in self._user_docs(self._tasks, user):
                if task.get("meeting_id") == meeting_id:
                    self._db.collection(self._tasks).document(task["id"]).delete()
            records = []
            for todo in todos:
                task_id = uuid.uuid4().hex[:12]
                record = {
                    "id": task_id,
                    "meeting_id": meeting_id,
                    "created_at": self._now(),
                    "user": user,
                    "status": "todo",
                    **todo,
                }
                self._db.collection(self._tasks).document(task_id).set(record)
                records.append(record)
            return records

    def delete_task(self, task_id: str, *, user: str = DEFAULT_USER) -> bool:
        with self._lock:
            ref = self._db.collection(self._tasks).document(task_id)
            snap = ref.get()
            if not snap.exists or not owns(snap.to_dict(), user):
                return False
            ref.delete()
            return True

    # ---- 備份 / 還原 ----

    def export_all(self, *, user: str = DEFAULT_USER) -> dict:
        return {
            "meetings": self._user_docs(self._meetings, user),
            "tasks": self._user_docs(self._tasks, user),
            "glossary": self.get_glossary(user=user),
            "speaker_roster": self.get_speaker_roster(user=user),
        }

    def import_all(self, data: dict, *, user: str = DEFAULT_USER) -> None:
        with self._lock:
            # 還原只能覆蓋自己的資料；別人的原封不動
            for coll in (self._meetings, self._tasks):
                for record in self._user_docs(coll, user):
                    if record.get("id"):
                        self._db.collection(coll).document(record["id"]).delete()
            for m in data.get("meetings", []):
                self._db.collection(self._meetings).document(m["id"]).set({**dict(m), "user": user})
            for t in data.get("tasks", []):
                t = {**dict(t), "user": user}
                t.setdefault("status", "todo")
                self._db.collection(self._tasks).document(t["id"]).set(t)
        if "glossary" in data:
            self.save_glossary(data.get("glossary") or [], user=user)
        if "speaker_roster" in data:
            self.save_speaker_roster(data.get("speaker_roster") or [], user=user)

    # ---- 自訂詞彙（meta collection 底下單一 glossary 文件） ----

    def _meta_doc(self, name: str) -> dict:
        snap = self._db.collection(self._meta).document(name).get()
        return snap.to_dict() if snap.exists else {}

    def get_glossary(self, *, user: str = DEFAULT_USER) -> list[dict]:
        return scoped_read(self._meta_doc("glossary"), user, "terms")

    def save_glossary(self, terms: list[dict], *, user: str = DEFAULT_USER) -> None:
        with self._lock:
            doc = scoped_write(self._meta_doc("glossary"), user, "terms", terms)
            self._db.collection(self._meta).document("glossary").set(doc)

    # ---- 講者名冊（meta collection 底下單一 speakers 文件） ----

    def get_speaker_roster(self, *, user: str = DEFAULT_USER) -> list[str]:
        return scoped_read(self._meta_doc("speakers"), user, "names")

    def save_speaker_roster(self, names: list[str], *, user: str = DEFAULT_USER) -> None:
        with self._lock:
            doc = scoped_write(self._meta_doc("speakers"), user, "names", names)
            self._db.collection(self._meta).document("speakers").set(doc)
