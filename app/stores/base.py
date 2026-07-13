"""任務儲存抽象介面。

8 月換成 Firebase Firestore 時，實作同一介面（FirestoreStore）即可，
上層的 Executor Agent 與 API 不需要改動。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import MeetingAnalysis


class TaskStore(ABC):
    @abstractmethod
    def save_meeting(self, analysis: MeetingAnalysis) -> str:
        """儲存一場會議的分析結果，回傳 meeting_id。"""

    @abstractmethod
    def get_meeting(self, meeting_id: str) -> dict | None: ...

    @abstractmethod
    def list_meetings(self) -> list[dict]:
        """所有會議（新到舊），不含任務明細。"""

    @abstractmethod
    def list_tasks(self, meeting_id: str | None = None) -> list[dict]:
        """攤平的代辦事項清單，可依會議過濾。"""
