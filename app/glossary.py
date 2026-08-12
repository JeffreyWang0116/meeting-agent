"""自訂詞彙表：人名、產品名等專有名詞。

轉錄（Gemini / Whisper）與分析（Decision Agent）的 prompt 都會帶上這份
詞彙表，「王霖翔」才不會被聽成「王林祥」，省去事後人工校正。

持久化交給 TaskStore（get_glossary / save_glossary）——本地走 JSON 檔、
雲端走 Firestore，與任務/會議同一後端，部署重啟也不會遺失。
"""
from __future__ import annotations

import threading

from app.stores.base import DEFAULT_USER

MAX_TERMS = 200


def glossary_prompt_line(terms: list[dict]) -> str:
    """把詞彙表串成 prompt 片段：「王霖翔（人名）、TaskHub」；空表回傳空字串。"""
    if not terms:
        return ""
    return "、".join(
        t["term"] + (f"（{t['note']}）" if t.get("note") else "") for t in terms
    )


def clean_terms(terms: list[dict], max_terms: int = MAX_TERMS) -> list[dict]:
    """歸一化詞彙清單：去空白、去重、擋掉過長。
    全域詞彙表與「本次專用詞彙」共用同一套規則，只是上限不同。"""
    cleaned, seen = [], set()
    for t in terms:
        if not isinstance(t, dict):
            raise ValueError("詞彙格式錯誤")
        term = str(t.get("term") or "").strip()
        note = str(t.get("note") or "").strip()
        if not term:
            raise ValueError("詞彙不可為空")
        if term in seen:
            continue
        seen.add(term)
        cleaned.append({"term": term, "note": note})
    if len(cleaned) > max_terms:
        raise ValueError(f"詞彙最多 {max_terms} 條")
    return cleaned


class Glossary:
    def __init__(self, store):
        self._store = store
        self._lock = threading.Lock()
        # 轉錄與分析每次都讀，快取避免高頻打資料庫。
        # 依使用者分開快取：未來多帳號時不能把 A 的詞彙餘給 B
        self._cache: dict[str, list[dict]] = {}

    def terms(self, user: str = DEFAULT_USER) -> list[dict]:
        with self._lock:
            if user not in self._cache:
                self._cache[user] = self._store.get_glossary(user=user)
            return [dict(t) for t in self._cache[user]]

    def replace(self, terms: list[dict], user: str = DEFAULT_USER) -> list[dict]:
        """整份取代（前端每次送完整清單，邏輯最單純）。回傳清理後的結果。"""
        cleaned = clean_terms(terms)
        with self._lock:
            self._store.save_glossary(cleaned, user=user)
            self._cache[user] = cleaned
        return [dict(t) for t in cleaned]
