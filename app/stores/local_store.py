"""本地 JSON 檔實作的 TaskStore。"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.models import MeetingAnalysis
from app.stores.base import TaskStore


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
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_meeting(
        self,
        analysis: MeetingAnalysis,
        transcript: str | None = None,
        kind: str | None = None,
    ) -> str:
        meeting_id = uuid.uuid4().hex[:12]
        created_at = datetime.now(timezone.utc).isoformat()
        dumped = analysis.model_dump(mode="json")

        meeting_record = {
            "id": meeting_id,
            "created_at": created_at,
            "meeting": dumped["meeting"],
            "decisions": dumped["decisions"],
            "pending_items": dumped["pending_items"],
            "transcript": transcript,
            "kind": kind,
            "tags": dumped.get("tags", []),
        }
        task_records = [
            {
                "id": uuid.uuid4().hex[:12],
                "meeting_id": meeting_id,
                "created_at": created_at,
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

    def get_meeting(self, meeting_id: str) -> dict | None:
        with self._lock:
            return next((m for m in self._data["meetings"] if m["id"] == meeting_id), None)

    def list_meetings(self) -> list[dict]:
        with self._lock:
            # 逐字稿可能數十 KB，列表回應剔除全文保持輕量（get_meeting 才回傳）
            return [
                {k: v for k, v in m.items() if k != "transcript"}
                for m in reversed(self._data["meetings"])
            ]

    def update_meeting(self, meeting_id: str, fields: dict) -> dict | None:
        with self._lock:
            for m in self._data["meetings"]:
                if m["id"] == meeting_id:
                    f = dict(fields)
                    nested = f.pop("meeting", None)
                    if nested:
                        m.setdefault("meeting", {}).update(nested)
                    m.update(f)
                    self._flush()
                    return dict(m)
        return None

    def delete_meeting(self, meeting_id: str) -> bool:
        with self._lock:
            before = len(self._data["meetings"])
            self._data["meetings"] = [m for m in self._data["meetings"] if m["id"] != meeting_id]
            if len(self._data["meetings"]) == before:
                return False
            self._data["tasks"] = [t for t in self._data["tasks"] if t["meeting_id"] != meeting_id]
            self._flush()
            return True

    def list_tasks(self, meeting_id: str | None = None) -> list[dict]:
        with self._lock:
            tasks = self._data["tasks"]
            if meeting_id is not None:
                tasks = [t for t in tasks if t["meeting_id"] == meeting_id]
            return list(tasks)

    def update_task(self, task_id: str, **fields) -> dict | None:
        with self._lock:
            for task in self._data["tasks"]:
                if task["id"] == task_id:
                    task.update(fields)
                    self._flush()
                    return dict(task)
        return None

    def replace_tasks(self, meeting_id: str, todos: list[dict]) -> list[dict]:
        created_at = datetime.now(timezone.utc).isoformat()
        records = [
            {
                "id": uuid.uuid4().hex[:12],
                "meeting_id": meeting_id,
                "created_at": created_at,
                "status": "todo",
                **todo,
            }
            for todo in todos
        ]
        with self._lock:
            self._data["tasks"] = [
                t for t in self._data["tasks"] if t["meeting_id"] != meeting_id
            ] + records
            self._flush()
        return [dict(r) for r in records]

    def delete_task(self, task_id: str) -> bool:
        with self._lock:
            before = len(self._data["tasks"])
            self._data["tasks"] = [t for t in self._data["tasks"] if t["id"] != task_id]
            if len(self._data["tasks"]) == before:
                return False
            self._flush()
            return True
