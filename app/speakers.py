"""講者名冊：記住這個使用者的會議裡常出現的人。

用途只有一個——餵給 SpeakerNamerAgent，讓它把「講者A」對應到姓名時，姓名的
寫法與過去幾場一致（「王霖翔」不會這場變「王林翔」下場變「王委員」）。

**不會餵給轉錄**：轉錄階段刻意只輸出代號，理由見 speaker_namer_agent 的模組
說明——模型沒有跨段記憶，給它姓名反而會讓同一個人在不同段落標成不同標籤。

名冊自動累積：AI 成功命名、或使用者在歷史會議手動改名時記一筆，最近用到的
排最前面，超過上限就從尾巴（最久沒出現的）淘汰。

持久化與詞彙表同樣交給 TaskStore（本地 JSON / 雲端 Firestore）。
"""
from __future__ import annotations

import threading

from app.agents.speaker_namer_agent import is_safe_name
from app.stores.base import DEFAULT_USER

MAX_NAMES = 100


def _clean(names) -> list[str]:
    """去空白、去重複，保持傳入順序。不判斷姓名安全性（各呼叫端自己決定要濾還是要報錯）。"""
    cleaned, seen = [], set()
    for raw in names:
        name = str(raw or "").strip()
        if name and name not in seen:
            seen.add(name)
            cleaned.append(name)
    return cleaned


class SpeakerRoster:
    def __init__(self, store):
        self._store = store
        self._lock = threading.Lock()
        # 每次講者辨識都要讀，快取避免頻繁打資料庫；寫入時同步更新
        self._cache: dict[str, list[str]] = {}

    def names(self, user: str = DEFAULT_USER) -> list[str]:
        with self._lock:
            if user not in self._cache:
                self._cache[user] = self._store.get_speaker_roster(user=user)
            return list(self._cache[user])

    def remember(self, names, user: str = DEFAULT_USER) -> list[str]:
        """把剛用到的姓名記進名冊（最近用到的排前面），回傳更新後的完整名冊。

        這是分析流程的副作用，**絕不拋例外**：名冊記不記得起來，都不該讓一場
        已經分析完的會議失敗。不合格的姓名（代號、含冒號、過長）直接略過。
        """
        fresh = [n for n in _clean(names) if is_safe_name(n)]
        if not fresh:
            return self.names(user)
        with self._lock:
            if user not in self._cache:
                self._cache[user] = self._store.get_speaker_roster(user=user)
            merged = fresh + [n for n in self._cache[user] if n not in set(fresh)]
            self._cache[user] = merged[:MAX_NAMES]
            self._store.save_speaker_roster(self._cache[user], user=user)
            return list(self._cache[user])

    def replace(self, names, user: str = DEFAULT_USER) -> list[str]:
        """整份取代（設定畫面每次送完整清單）。回傳清理後的結果。

        與 remember 相反，這裡的錯誤要讓使用者看見——手動輸入的東西默默消失
        比跳錯誤訊息更難理解。
        """
        if any(not str(n or "").strip() for n in names):
            raise ValueError("姓名不可為空")
        cleaned = _clean(names)  # 重複的姓名視為手滑，去掉即可，不算錯誤
        for name in cleaned:
            if not is_safe_name(name):
                raise ValueError(f"不能當作講者姓名：{name}")
        if len(cleaned) > MAX_NAMES:
            raise ValueError(f"講者名冊最多 {MAX_NAMES} 人")
        with self._lock:
            self._store.save_speaker_roster(cleaned, user=user)
            self._cache[user] = cleaned
        return list(cleaned)
