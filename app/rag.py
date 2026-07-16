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

from app.atomicio import atomic_write_text
from app.gemini_keys import KeyPool, call_with_rotation

# 向量維度：gemini-embedding-001 預設 3072 維，每場會議的索引 JSON 會膨脹到
# 數 MB。降到 768 維品質幾乎不變，索引小 4 倍、cosine 也快 4 倍。改這個值會
# 讓舊索引失效（維度不符），RagIndex 載入時偵測到就整份重建。
EMBED_DIM = 768


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
    def __init__(
        self,
        api_key=None,
        api_keys=None,
        model: str = "gemini-embedding-001",
        dim: int = EMBED_DIM,
    ):
        self._pool = KeyPool(api_keys if api_keys else [api_key])
        self.model = model
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self._pool:
            raise RagError(
                "未設定 GEMINI_API_KEY：跨會議問答需要 Gemini 金鑰做向量檢索"
            )
        return call_with_rotation(self._pool, lambda key: self._embed_with_key(key, texts))

    def _embed_with_key(self, key: str, texts: list[str]) -> list[list[float]]:
        from google import genai

        client = genai.Client(api_key=key)
        result = client.models.embed_content(
            model=self.model,
            contents=list(texts),
            config={"output_dimensionality": self.dim},
        )
        return [list(e.values) for e in result.embeddings]


class RagIndex:
    def __init__(self, path: Path | str, embedder):
        self._path = Path(path)
        self._embedder = embedder
        self._lock = threading.Lock()
        self._records = self._load()

    def _load(self) -> list[dict]:
        if not self._path.exists():
            return []
        data = json.loads(self._path.read_text(encoding="utf-8"))
        expected = getattr(self._embedder, "dim", None)
        # 向量維度改過（例如從 3072 降到 768）→ 舊向量與新問題向量不同長，
        # 直接作廢整份索引，下次 sync 用新維度重建。
        if expected is not None and data.get("dim") != expected:
            return []
        return data.get("records", [])

    def _flush(self) -> None:
        atomic_write_text(
            self._path,
            json.dumps(
                {"dim": getattr(self._embedder, "dim", None), "records": self._records},
                ensure_ascii=False,
            ),
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

    def reset(self) -> None:
        """清空整份索引（還原備份後呼叫：舊會議的向量已不再對應現有資料）。"""
        with self._lock:
            self._records = []
            self._flush()

    def drop_meeting(self, meeting_id: str) -> int:
        """把某場會議的片段從索引移除（會議被編輯/刪除時呼叫），
        回傳移除的片段數。編輯後下次 sync 會用新內容重建。"""
        with self._lock:
            before = len(self._records)
            self._records = [r for r in self._records if r["meeting_id"] != meeting_id]
            removed = before - len(self._records)
            if removed:
                self._flush()
            return removed

    def search(
        self, query: str, k: int = 4, meeting_ids: list[str] | None = None
    ) -> list[dict]:
        with self._lock:
            records = list(self._records)
        if meeting_ids is not None:  # 限定檢索範圍（詢問時複選會議）
            allowed = set(meeting_ids)
            records = [r for r in records if r["meeting_id"] in allowed]
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

    def ask(self, question: str, meeting_ids: list[str] | None = None) -> dict:
        question = question.strip()
        if not question:
            raise ValueError("問題不可為空")
        self._index.sync(self._store)
        hits = self._index.search(question, k=self.top_k, meeting_ids=meeting_ids)
        if not hits:
            message = (
                "所選會議中沒有可檢索的內容，換個範圍或先分析一場會議吧。"
                if meeting_ids is not None
                else "目前還沒有任何會議紀錄可供查詢，先分析一場會議吧。"
            )
            return {"answer": message, "sources": []}

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
