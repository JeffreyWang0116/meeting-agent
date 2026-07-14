"""任務儲存抽象介面。

8 月換成 Firebase Firestore 時，實作同一介面（FirestoreStore）即可，
上層的 Executor Agent 與 API 不需要改動。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import MeetingAnalysis


class TaskStore(ABC):
    @abstractmethod
    def save_meeting(
        self,
        analysis: MeetingAnalysis,
        transcript: str | None = None,
        kind: str | None = None,
    ) -> str:
        """儲存一場會議的分析結果（可附逐字稿原文供 RAG 檢索、錄音種類），回傳 meeting_id。"""

    @abstractmethod
    def get_meeting(self, meeting_id: str) -> dict | None: ...

    @abstractmethod
    def list_meetings(self) -> list[dict]:
        """所有會議（新到舊），不含任務明細。"""

    @abstractmethod
    def list_tasks(self, meeting_id: str | None = None) -> list[dict]:
        """攤平的代辦事項清單，可依會議過濾。"""

    @abstractmethod
    def update_task(self, task_id: str, **fields) -> dict | None:
        """更新任務欄位，回傳更新後的紀錄；找不到回傳 None。"""

    @abstractmethod
    def delete_task(self, task_id: str) -> bool:
        """刪除任務，回傳是否有刪到。"""
