"""RAG 跨會議問答：把歷史會議向量化，檢索相關片段後讓 Gemini 回答。

每場會議索引兩種內容：
1. 摘要卡 — 標題/摘要/決議/代辦/未決事項串成一段（舊會議沒逐字稿也可檢索）
2. 逐字稿切塊 — 固定長度、相鄰重疊，避免答案剛好被切斷

向量索引存本地 JSON（會議量是數十場等級，暴力餘弦相似即可，
不需要向量資料庫；8 月換 Firestore 時同介面替換）。
"""
from __future__ import annotations

import json
import math
import threading
from pathlib import Path

from app.gemini_keys import KeyPool, call_with_rotation


class RagError(Exception):
    pass


def chunk_text(text: str, size: int = 400, overlap: int = 80) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    step = size - overlap
    return [text[i : i + size] for i in range(0, len(text), step)]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _summary_card(meeting: dict, tasks: list[dict]) -> str:
    info = meeting.get("meeting", {})
    lines = [f"會議「{info.get('title', '')}」（{info.get('date', '')}）摘要：{info.get('summary', '')}"]
    for d in meeting.get("decisions", []):
        lines.append(f"決議：{d.get('description', '')}")
    for t in tasks:
        lines.append(
            f"代辦：{t.get('task', '')}（負責人：{t.get('owner') or '未定'}，"
            f"期限：{t.get('due_date') or '未定'}）"
        )
    for p in meeting.get("pending_items", []):
        lines.append(f"未決：{p.get('topic', '')}")
    return "\n".join(lines)


class GeminiEmbedder:
    def __init__(self, api_key=None, api_keys=None, model: str = "gemini-embedding-001"):
        self._pool = KeyPool(api_keys if api_keys else [api_key])
        self.model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self._pool:
            raise RagError(
                "未設定 GEMINI_API_KEY：跨會議問答需要 Gemini 金鑰做向量檢索"
            )
        return call_with_rotation(self._pool, lambda key: self._embed_with_key(key, texts))

    def _embed_with_key(self, key: str, texts: list[str]) -> list[list[float]]:
        from google import genai

        client = genai.Client(api_key=key)
        result = client.models.embed_content(model=self.model, contents=list(texts))
        return [list(e.values) for e in result.embeddings]


class RagIndex:
    def __init__(self, path: Path | str, embedder):
        self._path = Path(path)
        self._embedder = embedder
        self._lock = threading.Lock()
        self._records = self._load()

    def _load(self) -> list[dict]:
        if self._path.exists():
            return json.loads(self._path.read_text(encoding="utf-8"))["records"]
        return []

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"records": self._records}, ensure_ascii=False), encoding="utf-8"
        )

    def sync(self, store) -> int:
        """把還沒索引的會議切塊向量化，回傳新增的片段數。"""
        with self._lock:
            indexed = {r["meeting_id"] for r in self._records}
            added = 0
            for meeting in store.list_meetings():
                if meeting["id"] in indexed:
                    continue
                full = store.get_meeting(meeting["id"]) or meeting
                info = meeting.get("meeting", {})
                texts = [_summary_card(full, store.list_tasks(meeting_id=meeting["id"]))]
                texts += chunk_text(full.get("transcript") or "")
                vectors = self._embedder.embed(texts)
                for text, vector in zip(texts, vectors):
                    self._records.append(
                        {
                            "meeting_id": meeting["id"],
                            "title": info.get("title", ""),
                            "date": info.get("date", ""),
                            "text": text,
                            "vector": vector,
                        }
                    )
                added += len(texts)
            if added:
                self._flush()
            return added

    def search(self, query: str, k: int = 4) -> list[dict]:
        with self._lock:
            records = list(self._records)
        if not records:
            return []
        [qvec] = self._embedder.embed([query])
        scored = sorted(
            (dict(r, score=cosine(qvec, r["vector"])) for r in records),
            key=lambda r: r["score"],
            reverse=True,
        )
        return [
            {k_: r[k_] for k_ in ("meeting_id", "title", "date", "text", "score")}
            for r in scored[:k]
        ]


_ASK_PROMPT = """你是「主動式會議 Agent」的問答模組。根據以下歷史會議紀錄片段回答使用者的問題。

規則：
1. 只根據提供的片段回答；找不到答案就直說「在現有的會議紀錄中找不到相關資訊」，禁止編造。
2. 用繁體中文、3 句以內簡潔回答；人名與專有名詞保留原文寫法。
3. 提到具體事實時，註明出自哪場會議（標題與日期）。

會議紀錄片段：
---
{context}
---

問題：{question}"""


class AskAgent:
    def __init__(
        self,
        index: RagIndex,
        store,
        api_key=None,
        api_keys=None,
        model: str = "gemini-flash-latest",
        generate=None,
        top_k: int = 4,
    ):
        self._index = index
        self._store = store
        self._pool = KeyPool(api_keys if api_keys else [api_key])
        self.model = model
        self.top_k = top_k
        self._generate = generate or self._generate_with_gemini

    def ask(self, question: str) -> dict:
        question = question.strip()
        if not question:
            raise ValueError("問題不可為空")
        self._index.sync(self._store)
        hits = self._index.search(question, k=self.top_k)
        if not hits:
            return {"answer": "目前還沒有任何會議紀錄可供查詢，先分析一場會議吧。", "sources": []}

        context = "\n\n".join(f"【{h['title']}｜{h['date']}】\n{h['text']}" for h in hits)
        answer = (self._generate(_ASK_PROMPT.format(context=context, question=question)) or "").strip()

        sources, seen = [], set()
        for h in hits:
            if h["meeting_id"] in seen:
                continue
            seen.add(h["meeting_id"])
            sources.append({"meeting_id": h["meeting_id"], "title": h["title"], "date": h["date"]})
        return {"answer": answer, "sources": sources}

    def _generate_with_gemini(self, prompt: str) -> str:
        if not self._pool:
            raise RagError("未設定 GEMINI_API_KEY：跨會議問答需要 Gemini 金鑰")
        return call_with_rotation(self._pool, lambda key: self._call_gemini(key, prompt))

    def _call_gemini(self, key: str, prompt: str) -> str:
        from google import genai

        client = genai.Client(api_key=key)
        response = client.models.generate_content(
            model=self.model, contents=prompt, config={"temperature": 0.2}
        )
        return response.text or ""
