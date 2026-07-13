"""Executor Agent：資料庫與任務分發模組。

接收結構化分析結果並寫入任務庫。目前後端是本地 JSON（LocalJsonStore），
8 月換 Firestore 時只需注入不同的 TaskStore 實作。
"""
from __future__ import annotations

from app.models import MeetingAnalysis
from app.stores.base import TaskStore


class ExecutorAgent:
    def __init__(self, store: TaskStore):
        self.store = store

    def execute(self, analysis: MeetingAnalysis) -> str:
        return self.store.save_meeting(analysis)
