"""自訂詞彙表：人名、產品名等專有名詞。

轉錄（Gemini / Whisper）與分析（Decision Agent）的 prompt 都會帶上這份
詞彙表，「王霖翔」才不會被聽成「王林祥」，省去事後人工校正。

持久化交給 TaskStore（get_glossary / save_glossary）——本地走 JSON 檔、
雲端走 Firestore，與任務/會議同一後端，部署重啟也不會遺失。
"""
from __future__ import annotations

import threading

from app.agents.speaker_namer_agent import is_safe_name
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
        cleaned.append({"term": term, "note": note, "person": bool(t.get("person"))})
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

    def _load(self, user: str) -> list[dict]:
        """讀出詞彙表，順手把舊版另外存放的講者名冊搬進來標成人名。

        名冊原本是獨立的一份清單，使用者得維護兩處；合併後只留詞彙表這一份，
        標成人名的項目同時餵轉錄（別聽錯字）與講者命名（寫法一致）。
        沒搬的話舊使用者的名冊會像憑空消失。
        """
        terms = [
            {
                "term": str(t.get("term") or ""),
                "note": str(t.get("note") or ""),
                "person": bool(t.get("person")),
            }
            for t in self._store.get_glossary(user=user)
        ]
        try:
            legacy = self._store.get_speaker_roster(user=user)
        except Exception:  # 舊 store 沒有這個方法就當作沒有名冊要搬
            legacy = []
        if legacy:
            known = {t["term"] for t in terms}
            for t in terms:
                if t["term"] in set(legacy):
                    t["person"] = True
            terms += [
                {"term": n, "note": "", "person": True} for n in legacy if n not in known
            ]
            self._store.save_glossary(terms, user=user)
            self._store.save_speaker_roster([], user=user)  # 搬完清空，不再搬第二次
        return terms

    def terms(self, user: str = DEFAULT_USER) -> list[dict]:
        with self._lock:
            if user not in self._cache:
                self._cache[user] = self._load(user)
            return [dict(t) for t in self._cache[user]]

    def person_names(self, user: str = DEFAULT_USER) -> list[str]:
        """標成人名的詞彙——餵給 SpeakerNamerAgent，讓姓名寫法跨會議一致。"""
        return [t["term"] for t in self.terms(user) if t.get("person")]

    def remember_persons(self, names, user: str = DEFAULT_USER) -> None:
        """AI 命名成功時把姓名記進詞彙表並標為人名。

        這是分析流程的副作用，**絕不拋例外**：記不記得起來，都不該讓一場
        已經分析完的會議失敗。代號、含冒號、過長的姓名一律略過。
        自動加入永遠排在後面，也不會擠掉使用者手動整理的詞彙。
        """
        try:
            if not isinstance(names, (list, tuple, set)):
                return
            fresh = list(dict.fromkeys(
                n for n in (str(x or "").strip() for x in names)
                if n and is_safe_name(n)
            ))
            if not fresh:
                return
            with self._lock:
                if user not in self._cache:
                    self._cache[user] = self._load(user)
                current = self._cache[user]
                known = {t["term"] for t in current}
                for t in current:
                    if t["term"] in set(fresh):
                        t["person"] = True
                room = max(0, MAX_TERMS - len(current))
                current = current + [
                    {"term": n, "note": "", "person": True}
                    for n in fresh if n not in known
                ][:room]
                self._cache[user] = current
                self._store.save_glossary(current, user=user)
        except Exception:  # 名冊是加分項，靜靜略過
            pass

    def replace(self, terms: list[dict], user: str = DEFAULT_USER) -> list[dict]:
        """整份取代（前端每次送完整清單，邏輯最單純）。回傳清理後的結果。"""
        cleaned = clean_terms(terms)
        with self._lock:
            self._store.save_glossary(cleaned, user=user)
            self._cache[user] = cleaned
        return [dict(t) for t in cleaned]
