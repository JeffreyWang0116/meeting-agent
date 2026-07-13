"""API 用量統計：各類操作的今日/累計次數，落地成 JSON。

用來在前端儀表板顯示用量（Gemini 免費額度心安），也是專題口試的
量化數據來源（分析了幾場會議、轉錄了幾段音訊）。
"""
from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path


class UsageTracker:
    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            return json.loads(self._path.read_text(encoding="utf-8"))
        return {"total": {}, "daily": {}}

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def record(self, kind: str) -> None:
        today = date.today().isoformat()
        with self._lock:
            self._data["total"][kind] = self._data["total"].get(kind, 0) + 1
            day = self._data["daily"].setdefault(today, {})
            day[kind] = day.get(kind, 0) + 1
            self._flush()

    def snapshot(self) -> dict:
        today = date.today().isoformat()
        with self._lock:
            return {
                "today": dict(self._data["daily"].get(today, {})),
                "total": dict(self._data["total"]),
            }
