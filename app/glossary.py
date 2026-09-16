"""自訂詞彙表：人名、產品名等專有名詞。

轉錄（Gemini / Whisper）與分析（Decision Agent）的 prompt 都會帶上這份
詞彙表，「林佳蓉」才不會被聽成「林家容」，省去事後人工校正。

持久化交給 TaskStore（get_glossary / save_glossary）——本地走 JSON 檔、
雲端走 Firestore，與任務/會議同一後端，部署重啟也不會遺失。
"""
from __future__ import annotations

import threading

MAX_TERMS = 200


def glossary_prompt_line(terms: list[dict]) -> str:
    """把詞彙表串成 prompt 片段：「林佳蓉（人名）、TaskHub」；空表回傳空字串。"""
    if not terms:
        return ""
    return "、".join(
        t["term"] + (f"（{t['note']}）" if t.get("note") else "") for t in terms
    )


def terms_hint_line(terms: list[dict] | None, label: str = "已知詞彙表") -> str:
    """給轉錄 prompt 用的完整句子（空表回傳空字串）。

    全域詞彙表由 build_prompt 直接帶上，本次專用詞彙則走 transcribe() 的 hint
    參數——兩條路徑講的是同一件事，措辭集中在這裡，才不會改了一邊忘了另一邊。

    「只影響內文用字，講者標籤仍用代號」這句不能省：詞彙表裡有人名，模型看到
    人名就會想拿它當講者標籤用，而轉錄階段一律只輸出代號（見 _TRANSCRIBE_PROMPT）。
    """
    line = glossary_prompt_line(terms or [])
    if not line:
        return ""
    return (
        f"{label}（聽到相近發音時，人名與專有名詞一律採用以下寫法）：{line}。"
        "詞彙表只影響內文用字，講者標籤仍一律使用代號。"
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
    """user 一律必填，刻意不給預設值。

    給了 DEFAULT_USER 當預設值的話，少傳一個參數不會報錯，而是安靜地讀到
    另一個人的詞彙表——轉錄、校正、分析四條路徑曾經同時中這一刀，壞了一個
    月都沒有徵兆，因為症狀只是「別人的人名出現在我的會議裡」。

    這一層的回傳值會直接進 prompt，所以寧可在呼叫端就 TypeError：CI 當場
    擋下來，好過變成線上的跨帳號資料外洩。
    """

    def __init__(self, store):
        self._store = store
        self._lock = threading.Lock()
        # 轉錄與分析每次都讀，快取避免高頻打資料庫。
        # 依使用者分開快取：未來多帳號時不能把 A 的詞彙餘給 B
        self._cache: dict[str, list[dict]] = {}

    def _load(self, user: str) -> list[dict]:
        """讀出詞彙表，順手把舊版另外存放的講者名冊搬進來。

        名冊原本用來統一 AI 對應出的講者姓名；系統已不再自動對應姓名（講者一律
        代號、使用者事後改名），「人名」標記也跟著移除，舊資料帶的 person 一律丟掉。
        名冊裡的姓名仍然有用——轉錄時不會被聽錯字——所以搬成一般詞彙、註明人名。
        沒搬的話舊使用者的名冊會像憑空消失。
        """
        terms = [
            {"term": str(t.get("term") or ""), "note": str(t.get("note") or "")}
            for t in self._store.get_glossary(user=user)
        ]
        try:
            legacy = self._store.get_speaker_roster(user=user)
        except Exception:  # 舊 store 沒有這個方法就當作沒有名冊要搬
            legacy = []
        if legacy:
            known = {t["term"] for t in terms}
            terms += [{"term": n, "note": "人名"} for n in legacy if n not in known]
            self._store.save_glossary(terms, user=user)
            self._store.save_speaker_roster([], user=user)  # 搬完清空，不再搬第二次
        return terms

    def terms(self, user: str) -> list[dict]:
        with self._lock:
            if user not in self._cache:
                self._cache[user] = self._load(user)
            return [dict(t) for t in self._cache[user]]

    def replace(self, terms: list[dict], user: str) -> list[dict]:
        """整份取代（前端每次送完整清單，邏輯最單純）。回傳清理後的結果。"""
        cleaned = clean_terms(terms)
        with self._lock:
            self._store.save_glossary(cleaned, user=user)
            self._cache[user] = cleaned
        return [dict(t) for t in cleaned]
