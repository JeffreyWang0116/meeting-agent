"""任務儲存抽象介面。

8 月換成 Firebase Firestore 時，實作同一介面（FirestoreStore）即可，
上層的 Executor Agent 與 API 不需要改動。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import MeetingAnalysis


# 使用者歸屬（多租戶鋪墊）。現在整站只有這一個使用者，但每筆寫入都會蓋上它，
# 讀取也一律照它過濾——之後接真正的登入（如 Firebase Auth）時只要換掉
# main.py 的 current_user()，不必遷移任何既有資料。
# 舊資料沒有 user 欄位，一律視為 DEFAULT_USER 的（見 owns）。
DEFAULT_USER = "local"


def owns(record: dict, user: str) -> bool:
    return record.get("user", DEFAULT_USER) == user


def scoped_read(data: dict, user: str, key: str) -> list:
    """詞彙表／講者名冊這種「單一文件」的讀取：新格式放在 by_user 底下，
    舊格式是頂層的 terms/names，只屬於 DEFAULT_USER。"""
    if "by_user" in data:
        return data["by_user"].get(user, [])
    return data.get(key, []) if user == DEFAULT_USER else []


def scoped_write(data: dict, user: str, key: str, value: list) -> dict:
    """回傳寫入後的完整文件內容；順手把舊格式搬進 by_user，不留兩種格式並存。"""
    by_user = dict(data.get("by_user") or {})
    if "by_user" not in data and data.get(key):
        by_user[DEFAULT_USER] = data[key]
    by_user[user] = value
    return {"by_user": by_user}


class TaskStore(ABC):
    @abstractmethod
    def save_meeting(
        self,
        analysis: MeetingAnalysis,
        transcript: str | None = None,
        kind: str | None = None,
        terms: list[dict] | None = None,
        user: str = DEFAULT_USER,
    ) -> str:
        """儲存一場會議的分析結果（可附逐字稿原文供 RAG 檢索、會議種類、本次專用詞彙），回傳 meeting_id。"""

    @abstractmethod
    def get_meeting(self, meeting_id: str, *, user: str = DEFAULT_USER) -> dict | None: ...

    @abstractmethod
    def list_meetings(self, *, user: str = DEFAULT_USER) -> list[dict]:
        """所有會議（新到舊），不含任務明細。"""

    @abstractmethod
    def update_meeting(self, meeting_id: str, fields: dict, *, user: str = DEFAULT_USER) -> dict | None:
        """更新會議紀錄。fields 的 "meeting" 子字典會併入會議資訊
        （title/date/summary/attendees），其餘鍵（transcript/kind/tags…）
        直接覆寫頂層欄位。找不到回傳 None。"""

    @abstractmethod
    def delete_meeting(self, meeting_id: str, *, user: str = DEFAULT_USER) -> bool:
        """刪除會議與其所有任務，回傳是否有刪到。"""

    @abstractmethod
    def list_tasks(self, meeting_id: str | None = None, *, user: str = DEFAULT_USER) -> list[dict]:
        """攤平的代辦事項清單，可依會議過濾。"""

    @abstractmethod
    def add_task(self, task: dict, *, user: str = DEFAULT_USER) -> dict:
        """新增一筆手動任務（非 AI 分析產生），補上 id/created_at/status，回傳完整紀錄。
        手動任務的 meeting_id 為 None（不屬於任何一場會議）。"""

    @abstractmethod
    def update_task(self, task_id: str, *, user: str = DEFAULT_USER, **fields) -> dict | None:
        """更新任務欄位，回傳更新後的紀錄；找不到回傳 None。"""

    @abstractmethod
    def delete_task(self, task_id: str, *, user: str = DEFAULT_USER) -> bool:
        """刪除任務，回傳是否有刪到。"""

    @abstractmethod
    def replace_tasks(self, meeting_id: str, todos: list[dict], *, user: str = DEFAULT_USER) -> list[dict]:
        """整批替換某場會議的任務（重新分析時使用），回傳新任務紀錄。"""

    @abstractmethod
    def get_glossary(self, *, user: str = DEFAULT_USER) -> list[dict]:
        """讀取自訂詞彙表（[{"term","note"}, ...]），沒有回傳空清單。"""

    @abstractmethod
    def save_glossary(self, terms: list[dict], *, user: str = DEFAULT_USER) -> None:
        """整份儲存自訂詞彙表。"""

    @abstractmethod
    def get_speaker_roster(self, *, user: str = DEFAULT_USER) -> list[str]:
        """讀取講者名冊（["王霖翔", ...]，最近用到的在前），沒有回傳空清單。"""

    @abstractmethod
    def save_speaker_roster(self, names: list[str], *, user: str = DEFAULT_USER) -> None:
        """整份儲存講者名冊。"""

    @abstractmethod
    def export_all(self, *, user: str = DEFAULT_USER) -> dict:
        """匯出整份資料（meetings + tasks + glossary + speaker_roster）供備份下載。"""

    @abstractmethod
    def import_all(self, data: dict, *, user: str = DEFAULT_USER) -> None:
        """以備份資料整份覆蓋現有資料（還原備份時使用）。"""
