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
    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            return json.loads(self._path.read_text(encoding="utf-8"))
        return {"meetings": [], "tasks": []}

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_meeting(self, analysis: MeetingAnalysis) -> str:
        meeting_id = uuid.uuid4().hex[:12]
        created_at = datetime.now(timezone.utc).isoformat()
        dumped = analysis.model_dump(mode="json")

        meeting_record = {
            "id": meeting_id,
            "created_at": created_at,
            "meeting": dumped["meeting"],
            "decisions": dumped["decisions"],
            "pending_items": dumped["pending_items"],
        }
        task_records = [
            {"id": uuid.uuid4().hex[:12], "meeting_id": meeting_id, "created_at": created_at, **todo}
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
            return list(reversed(self._data["meetings"]))

    def list_tasks(self, meeting_id: str | None = None) -> list[dict]:
        with self._lock:
            tasks = self._data["tasks"]
            if meeting_id is not None:
                tasks = [t for t in tasks if t["meeting_id"] == meeting_id]
            return list(tasks)
