"""本地 JSON 檔實作的 TaskStore。"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.atomicio import atomic_write_text
from app.models import MeetingAnalysis
from app.stores.base import DEFAULT_USER, TaskStore, owns, scoped_read, scoped_write

# 手動任務可填的欄位（與 AI 產出的任務同型別，缺的補預設）
_MANUAL_TASK_FIELDS = ("task", "owner", "due_date", "priority", "source_quote")


def _new_task_record(task: dict) -> dict:
    """把使用者手動輸入的任務正規化成與 AI 任務一致的完整紀錄。"""
    record = {
        "id": uuid.uuid4().hex[:12],
        "meeting_id": task.get("meeting_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": task.get("status") or "todo",
        "priority": task.get("priority") or "medium",
    }
    for field_name in _MANUAL_TASK_FIELDS:
        record.setdefault(field_name, task.get(field_name))
    return record


class LocalJsonStore(TaskStore):
    backend = "local"

    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            data = json.loads(self._path.read_text(encoding="utf-8"))
        else:
            data = {"meetings": [], "tasks": []}
        for task in data["tasks"]:  # 舊版資料沒有 status 欄位，補預設值
            task.setdefault("status", "todo")
        return data

    def _flush(self) -> None:
        atomic_write_text(
            self._path, json.dumps(self._data, ensure_ascii=False, indent=2)
        )

    def save_meeting(
        self,
        analysis: MeetingAnalysis,
        transcript: str | None = None,
        kind: str | None = None,
        terms: list[dict] | None = None,
        user: str = DEFAULT_USER,
    ) -> str:
        meeting_id = uuid.uuid4().hex[:12]
        created_at = datetime.now(timezone.utc).isoformat()
        dumped = analysis.model_dump(mode="json")

        meeting_record = {
            "id": meeting_id,
            "created_at": created_at,
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
        }
        task_records = [
            {
                "id": uuid.uuid4().hex[:12],
                "meeting_id": meeting_id,
                "created_at": created_at,
                "user": user,
                "status": "todo",
                **todo,
            }
            for todo in dumped["todos"]
        ]

        with self._lock:
            self._data["meetings"].append(meeting_record)
            self._data["tasks"].extend(task_records)
            self._flush()
        return meeting_id

    def get_meeting(self, meeting_id: str, *, user: str = DEFAULT_USER) -> dict | None:
        with self._lock:
            return next(
                (m for m in self._data["meetings"] if m["id"] == meeting_id and owns(m, user)),
                None,
            )

    def list_meetings(self, *, user: str = DEFAULT_USER) -> list[dict]:
        with self._lock:
            # 逐字稿可能數十 KB，列表回應剔除全文保持輕量（get_meeting 才回傳）
            return [
                {k: v for k, v in m.items() if k != "transcript"}
                for m in reversed(self._data["meetings"])
                if owns(m, user)
            ]

    def update_meeting(self, meeting_id: str, fields: dict, *, user: str = DEFAULT_USER) -> dict | None:
        with self._lock:
            for m in self._data["meetings"]:
                if m["id"] == meeting_id and owns(m, user):
                    f = dict(fields)
                    nested = f.pop("meeting", None)
                    if nested:
                        m.setdefault("meeting", {}).update(nested)
                    m.update(f)
                    self._flush()
                    return dict(m)
        return None

    def delete_meeting(self, meeting_id: str, *, user: str = DEFAULT_USER) -> bool:
        with self._lock:
            before = len(self._data["meetings"])
            self._data["meetings"] = [
                m for m in self._data["meetings"]
                if not (m["id"] == meeting_id and owns(m, user))
            ]
            if len(self._data["meetings"]) == before:
                return False
            self._data["tasks"] = [t for t in self._data["tasks"] if t["meeting_id"] != meeting_id]
            self._flush()
            return True

    def list_tasks(self, meeting_id: str | None = None, *, user: str = DEFAULT_USER) -> list[dict]:
        with self._lock:
            tasks = [t for t in self._data["tasks"] if owns(t, user)]
            if meeting_id is not None:
                tasks = [t for t in tasks if t["meeting_id"] == meeting_id]
            return list(tasks)

    def add_task(self, task: dict, *, user: str = DEFAULT_USER) -> dict:
        record = _new_task_record(task)
        record["user"] = user
        with self._lock:
            self._data["tasks"].append(record)
            self._flush()
        return dict(record)

    def update_task(self, task_id: str, *, user: str = DEFAULT_USER, **fields) -> dict | None:
        with self._lock:
            for task in self._data["tasks"]:
                if task["id"] == task_id and owns(task, user):
                    task.update(fields)
                    self._flush()
                    return dict(task)
        return None

    def replace_tasks(self, meeting_id: str, todos: list[dict], *, user: str = DEFAULT_USER) -> list[dict]:
        created_at = datetime.now(timezone.utc).isoformat()
        records = [
            {
                "id": uuid.uuid4().hex[:12],
                "meeting_id": meeting_id,
                "created_at": created_at,
                "user": user,
                "status": "todo",
                **todo,
            }
            for todo in todos
        ]
        with self._lock:
            self._data["tasks"] = [
                t for t in self._data["tasks"]
                if not (t["meeting_id"] == meeting_id and owns(t, user))
            ] + records
            self._flush()
        return [dict(r) for r in records]

    def delete_task(self, task_id: str, *, user: str = DEFAULT_USER) -> bool:
        with self._lock:
            before = len(self._data["tasks"])
            self._data["tasks"] = [
                t for t in self._data["tasks"]
                if not (t["id"] == task_id and owns(t, user))
            ]
            if len(self._data["tasks"]) == before:
                return False
            self._flush()
            return True

    # ---- 備份 / 還原 ----

    def export_all(self, *, user: str = DEFAULT_USER) -> dict:
        with self._lock:
            return {
                "meetings": [dict(m) for m in self._data["meetings"] if owns(m, user)],
                "tasks": [dict(t) for t in self._data["tasks"] if owns(t, user)],
                "glossary": self.get_glossary(user=user),
                "speaker_roster": self.get_speaker_roster(user=user),
            }

    def import_all(self, data: dict, *, user: str = DEFAULT_USER) -> None:
        # 還原只能覆蓋自己的資料；別人的原封不動地留下來
        meetings = [{**dict(m), "user": user} for m in data.get("meetings", [])]
        tasks = [{**dict(t), "user": user} for t in data.get("tasks", [])]
        for t in tasks:
            t.setdefault("status", "todo")
        with self._lock:
            self._data = {
                "meetings": [m for m in self._data["meetings"] if not owns(m, user)] + meetings,
                "tasks": [t for t in self._data["tasks"] if not owns(t, user)] + tasks,
            }
            self._flush()
        if "glossary" in data:
            self.save_glossary(data.get("glossary") or [], user=user)
        if "speaker_roster" in data:
            self.save_speaker_roster(data.get("speaker_roster") or [], user=user)

    # ---- 自訂詞彙（沿用同目錄的 glossary.json，與 db.json 並存） ----

    def _glossary_path(self):
        return self._path.with_name("glossary.json")

    def _read_doc(self, path) -> dict:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def get_glossary(self, *, user: str = DEFAULT_USER) -> list[dict]:
        return scoped_read(self._read_doc(self._glossary_path()), user, "terms")

    def save_glossary(self, terms: list[dict], *, user: str = DEFAULT_USER) -> None:
        path = self._glossary_path()
        atomic_write_text(
            path,
            json.dumps(scoped_write(self._read_doc(path), user, "terms", terms),
                       ensure_ascii=False, indent=2),
        )

    # ---- 講者名冊（同目錄的 speakers.json） ----

    def _roster_path(self):
        return self._path.with_name("speakers.json")

    def get_speaker_roster(self, *, user: str = DEFAULT_USER) -> list[str]:
        return scoped_read(self._read_doc(self._roster_path()), user, "names")

    def save_speaker_roster(self, names: list[str], *, user: str = DEFAULT_USER) -> None:
        path = self._roster_path()
        atomic_write_text(
            path,
            json.dumps(scoped_write(self._read_doc(path), user, "names", names),
                       ensure_ascii=False, indent=2),
        )
