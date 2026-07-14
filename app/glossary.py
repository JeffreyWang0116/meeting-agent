"""自訂詞彙表：人名、產品名等專有名詞。

轉錄（Gemini / Whisper）與分析（Decision Agent）的 prompt 都會帶上這份
詞彙表，「王霖翔」才不會被聽成「王林祥」，省去事後人工校正。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

MAX_TERMS = 200


def glossary_prompt_line(terms: list[dict]) -> str:
    """把詞彙表串成 prompt 片段：「王霖翔（人名）、TaskHub」；空表回傳空字串。"""
    if not terms:
        return ""
    return "、".join(
        t["term"] + (f"（{t['note']}）" if t.get("note") else "") for t in terms
    )


class Glossary:
    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._terms: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if self._path.exists():
            return json.loads(self._path.read_text(encoding="utf-8"))["terms"]
        return []

    def terms(self) -> list[dict]:
        with self._lock:
            return [dict(t) for t in self._terms]

    def replace(self, terms: list[dict]) -> list[dict]:
        """整份取代（前端每次送完整清單，邏輯最單純）。回傳清理後的結果。"""
        cleaned, seen = [], set()
        for t in terms:
            term = str(t.get("term") or "").strip()
            note = str(t.get("note") or "").strip()
            if not term:
                raise ValueError("詞彙不可為空")
            if term in seen:
                continue
            seen.add(term)
            cleaned.append({"term": term, "note": note})
        if len(cleaned) > MAX_TERMS:
            raise ValueError(f"詞彙最多 {MAX_TERMS} 條")
        with self._lock:
            self._terms = cleaned
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"terms": cleaned}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return self.terms()
