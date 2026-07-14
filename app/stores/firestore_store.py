"""Firebase Firestore 實作的 TaskStore（雲端持久化）。

與 LocalJsonStore 行為一致、實作同一介面，可在 create_app 直接互換：
本機開發不填金鑰用 JSON，雲端填了 Firebase 金鑰就換成這個，資料就不會
在 Render 重新部署時被清空。

資料模型：兩個 collection——`meetings`（每場會議一份文件，含逐字稿全文）、
`tasks`（每筆代辦一份文件，帶 meeting_id）。會議量是數十場等級，排序與
過濾直接在 Python 做，不依賴 Firestore 複合索引（省去建索引的設定）。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

from app.models import MeetingAnalysis
from app.stores.base import TaskStore


class FirestoreStore(TaskStore):
    backend = "firestore"

    def __init__(self, db, *, meetings: str = "meetings", tasks: str = "tasks"):
        self._db = db
        self._meetings = meetings
        self._tasks = tasks
        self._lock = threading.Lock()
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

    # ---- TaskStore 介面 ----

    def save_meeting(
        self,
        analysis: MeetingAnalysis,
        transcript: str | None = None,
        kind: str | None = None,
    ) -> str:
        meeting_id = uuid.uuid4().hex[:12]
        dumped = analysis.model_dump(mode="json")

        with self._lock:
            self._db.collection(self._meetings).document(meeting_id).set({
                "id": meeting_id,
                "created_at": self._now(),
                "meeting": dumped["meeting"],
                "decisions": dumped["decisions"],
                "pending_items": dumped["pending_items"],
                "transcript": transcript,
                "kind": kind,
            })
            for todo in dumped["todos"]:
                task_id = uuid.uuid4().hex[:12]
                self._db.collection(self._tasks).document(task_id).set({
                    "id": task_id,
                    "meeting_id": meeting_id,
                    "created_at": self._now(),
                    "status": "todo",
                    **todo,
                })
        return meeting_id

    def get_meeting(self, meeting_id: str) -> dict | None:
        snap = self._db.collection(self._meetings).document(meeting_id).get()
        return snap.to_dict() if snap.exists else None

    def list_meetings(self) -> list[dict]:
        docs = [s.to_dict() for s in self._db.collection(self._meetings).stream()]
        docs.sort(key=lambda m: m.get("created_at", ""), reverse=True)  # 新到舊
        # 逐字稿可能數十 KB，列表回應剔除全文保持輕量（get_meeting 才回傳）
        return [{k: v for k, v in m.items() if k != "transcript"} for m in docs]

    def update_meeting(self, meeting_id: str, fields: dict) -> dict | None:
        with self._lock:
            ref = self._db.collection(self._meetings).document(meeting_id)
            snap = ref.get()
            if not snap.exists:
                return None
            merged = snap.to_dict()
            f = dict(fields)
            nested = f.pop("meeting", None)
            if nested:
                merged.setdefault("meeting", {}).update(nested)
            merged.update(f)
            ref.set(merged)
            return merged

    def delete_meeting(self, meeting_id: str) -> bool:
        with self._lock:
            ref = self._db.collection(self._meetings).document(meeting_id)
            if not ref.get().exists:
                return False
            ref.delete()
            for snap in self._db.collection(self._tasks).stream():
                task = snap.to_dict()
                if task.get("meeting_id") == meeting_id:
                    self._db.collection(self._tasks).document(task["id"]).delete()
            return True

    def list_tasks(self, meeting_id: str | None = None) -> list[dict]:
        docs = [s.to_dict() for s in self._db.collection(self._tasks).stream()]
        if meeting_id is not None:
            docs = [t for t in docs if t.get("meeting_id") == meeting_id]
        docs.sort(key=lambda t: t.get("created_at", ""))  # 建立順序
        for t in docs:  # 舊資料沒有 status 欄位，補預設值
            t.setdefault("status", "todo")
        return docs

    def update_task(self, task_id: str, **fields) -> dict | None:
        with self._lock:
            ref = self._db.collection(self._tasks).document(task_id)
            snap = ref.get()
            if not snap.exists:
                return None
            ref.update(fields)
            merged = snap.to_dict()
            merged.update(fields)
            merged.setdefault("status", "todo")
            return merged

    def delete_task(self, task_id: str) -> bool:
        with self._lock:
            ref = self._db.collection(self._tasks).document(task_id)
            if not ref.get().exists:
                return False
            ref.delete()
            return True
