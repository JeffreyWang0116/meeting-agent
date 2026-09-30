"""RAG 跨會議問答：把歷史會議向量化，檢索相關片段後讓 Gemini 回答。

每場會議索引兩種內容：
1. 摘要卡 — 標題/摘要/決議/代辦/未決事項串成一段（舊會議沒逐字稿也可檢索）
2. 逐字稿切塊 — 依行切（不把一句話切斷）、相鄰重疊一行，並記下第一個時間戳，
   問答列出的段落才跳得回逐字稿那一行

向量索引交給 TaskStore（會議量是數十場等級，暴力餘弦相似即可，不需要
向量資料庫）：本地走 JSON 檔、雲端走 Firestore，與會議／任務同一後端，
所以雲端重新部署之後索引還在，不必整份重新向量化。

多租戶：索引檔是全站共用的一份，所以每筆記錄都要蓋上 user，sync 讀 store
時也要帶 user。少了這一層，接上登入之後 A 的問題會檢索到 B 的會議內容——
store 那一層已經隔離了，這裡是整條資料流唯一會漏的地方。
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading

from app.agents.decision_agent import strip_code_fence
from app.ask_planner import AskPlan, build_plan_prompt, filter_meetings, parse_plan
from app.gemini_keys import KeyPool, call_with_model_fallback, call_with_rotation
from app.stores.base import DEFAULT_USER
from app.timeutil import today_local

logger = logging.getLogger(__name__)

# 向量維度：gemini-embedding-001 預設 3072 維，每場會議的索引 JSON 會膨脹到
# 數 MB。降到 768 維品質幾乎不變，索引小 4 倍、cosine 也快 4 倍。改這個值會
# 讓舊索引失效（維度不符），RagIndex 載入時偵測到就整份重建。
EMBED_DIM = 768


class RagError(Exception):
    pass


_TIME_LABEL = re.compile(r"^\s*\[(\d{1,2}(?::\d{1,2}){0,2})\]")


def chunk_transcript(text: str, size: int = 400) -> list[dict]:
    """逐字稿依行切成不超過 size 字的片段，相鄰片段重疊一行；每段帶第一個時間戳。"""
    pieces: list[tuple[str, str | None]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _TIME_LABEL.match(line)
        time = m.group(1) if m else None
        # 單行就超過上限（沒換行的長逐字稿）只能硬切，切出來的每段沿用這行的時間
        pieces += [(line[i : i + size], time) for i in range(0, len(line), size)]

    chunks, current = [], []

    def emit():
        chunks.append({
            "text": "\n".join(t for t, _ in current),
            "time": next((tm for _, tm in current if tm), None),
        })

    for piece in pieces:
        if current and len("\n".join(t for t, _ in current + [piece])) > size:
            emit()
            last = current[-1]
            current = [last] if len(last[0]) + 1 + len(piece[0]) <= size else []
        current.append(piece)
    if current:
        emit()
    return chunks


def focus_line(text: str, keywords: list[str], answer: str) -> tuple[str | None, str]:
    """片段裡最能回答問題的那一行（時間、去掉時間戳的內容）。

    一個片段有好幾行，點了若只跳到片段開頭，使用者還得自己往下找。依序比：命中的
    關鍵詞數、與回答共用的二字詞數；都沒有就取第一行。沒時間戳的行沿用上一個時間。
    """
    words = [w.lower() for w in keywords if w.strip()]
    grams = {answer[i : i + 2] for i in range(len(answer) - 1)} - {""}
    best, best_score, last_time = (None, ""), -1, None
    for line in text.splitlines():
        m = _TIME_LABEL.match(line)
        if m:
            last_time = m.group(1)
        content = line[m.end():].strip() if m else line.strip()
        if not content:
            continue
        lowered = content.lower()
        score = 1000 * sum(w in lowered for w in words) + sum(g in content for g in grams)
        if score > best_score:
            best, best_score = (last_time, content), score
    return best


def _record_user(record: dict) -> str:
    """索引記錄的歸屬。改版前存下來的記錄沒有 user 欄位，與 store 的 owns()
    一致視為 DEFAULT_USER 的——否則舊索引會整份無聲失效。"""
    return record.get("user", DEFAULT_USER)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _summary_card(meeting: dict, tasks: list[dict]) -> str:
    info = meeting.get("meeting", {})
    lines = [f"會議「{info.get('title', '')}」（{info.get('date', '')}）摘要：{info.get('summary') or ''}"]
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
        on_call=None,
        model: str = "gemini-embedding-001",
        dim: int = EMBED_DIM,
    ):
        self._pool = KeyPool(api_keys if api_keys else [api_key])
        # 每打一次 API 就回報一次，用量統計才會算到重試與換金鑰
        self._on_call = on_call
        self.model = model
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self._pool:
            raise RagError(
                "未設定 GEMINI_API_KEY：跨會議問答需要 Gemini 金鑰做向量檢索"
            )
        return call_with_rotation(
            self._pool, lambda key: self._embed_with_key(key, texts), on_call=self._on_call
        )

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
    """索引存哪裡交給 store：本地是 JSON 檔，雲端是 Firestore。

    原本固定寫本地檔案，而雲端免費方案沒有持久磁碟——每次部署索引就整份
    消失，下一個提問要把所有會議重新向量化，既慢又吃 embedding 額度。

    記錄仍整份載進記憶體、檢索也在記憶體做：載入只發生在建構當下（一個
    process 一次），提問時不會再打資料庫。
    """

    def __init__(self, store, embedder):
        self._store = store
        self._embedder = embedder
        self._lock = threading.Lock()
        self._records = self._load()

    def _load(self) -> list[dict]:
        data = self._store.get_rag_records()
        expected = getattr(self._embedder, "dim", None)
        # 向量維度改過（例如從 3072 降到 768）→ 舊向量與新問題向量不同長，
        # 直接作廢整份索引，下次 sync 用新維度重建。
        if expected is not None and data.get("dim") != expected:
            return []
        records = data.get("records", [])
        # 改版前的逐字稿切塊固定 400 字、沒有時間戳，列出的段落跳不回原文 → 整份重建
        if any("source" not in r for r in records):
            return []
        return records

    def _flush(self) -> None:
        self._store.save_rag_records(
            getattr(self._embedder, "dim", None), self._records
        )

    def sync(self, store, user: str = DEFAULT_USER) -> int:
        """把這位使用者還沒索引的會議切塊向量化，回傳新增的片段數。"""
        with self._lock:
            indexed = {r["meeting_id"] for r in self._records}
            added = 0
            for meeting in store.list_meetings(user=user):
                if meeting["id"] in indexed:
                    continue
                full = store.get_meeting(meeting["id"], user=user) or meeting
                info = meeting.get("meeting", {})
                tasks = store.list_tasks(meeting_id=meeting["id"], user=user)
                pieces = [{"text": _summary_card(full, tasks), "time": None, "source": "summary"}]
                pieces += [
                    dict(c, source="transcript")
                    for c in chunk_transcript(full.get("transcript") or "")
                ]
                vectors = self._embedder.embed([p["text"] for p in pieces])
                for piece, vector in zip(pieces, vectors):
                    self._records.append(
                        {
                            "meeting_id": meeting["id"],
                            "user": user,
                            "title": info.get("title", ""),
                            "date": info.get("date", ""),
                            **piece,
                            "vector": vector,
                        }
                    )
                added += len(pieces)
            if added:
                self._flush()
            return added

    def reset(self, user: str | None = None) -> None:
        """清空索引（還原備份後呼叫：舊會議的向量已不再對應現有資料）。

        還原是單一使用者的動作，所以預設只清那個人的；user=None 才是整份清空。
        """
        with self._lock:
            self._records = (
                []
                if user is None
                else [r for r in self._records if _record_user(r) != user]
            )
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

    def _visible(self, meeting_ids: list[str] | None, user: str) -> list[dict]:
        with self._lock:
            # 先擋掉別人的記錄，再套 meeting_ids——順序不能反過來，否則指名
            # 別人的 meeting_id 就能把對方的片段撈出來
            records = [r for r in self._records if _record_user(r) == user]
        if meeting_ids is not None:  # 限定檢索範圍（詢問時複選會議、AI 解析的條件）
            allowed = set(meeting_ids)
            records = [r for r in records if r["meeting_id"] in allowed]
        return records

    def search(
        self,
        query: str,
        k: int = 4,
        meeting_ids: list[str] | None = None,
        user: str = DEFAULT_USER,
    ) -> list[dict]:
        records = self._visible(meeting_ids, user)
        if not records:
            return []
        [qvec] = self._embedder.embed([query])
        scored = sorted(
            (dict(r, score=cosine(qvec, r["vector"])) for r in records),
            key=lambda r: r["score"],
            reverse=True,
        )
        return [_hit(r) for r in scored[:k]]

    def keyword_search(
        self,
        keywords: list[str],
        k: int = 4,
        meeting_ids: list[str] | None = None,
        user: str = DEFAULT_USER,
    ) -> list[dict]:
        """逐字出現的關鍵詞（不分大小寫），命中越多詞的排越前面。不打 API。

        向量檢索對人名、產品代號、數字這類「要一字不差」的詞很弱，這裡補上。
        """
        words = [w.lower() for w in keywords if w.strip()]
        if not words:
            return []
        scored = []
        for r in self._visible(meeting_ids, user):
            text = r["text"].lower()
            count = sum(w in text for w in words)
            if count:
                scored.append(dict(r, score=float(count)))
        scored.sort(key=lambda r: r["score"], reverse=True)  # sort 是穩定的：同分維持原順序
        return [_hit(r) for r in scored[:k]]


def _hit(record: dict) -> dict:
    fields = ("meeting_id", "title", "date", "text", "time", "source", "score")
    return {f: record.get(f) for f in fields}


# 模型答不出來時要說的固定句。提示規定它、回傳前也用它判斷該不該列來源，所以
# 抽成常數——兩邊各寫一次的話，改了提示的用字，來源判斷會無聲地失效
NOT_FOUND_ANSWER = "在現有的會議紀錄中找不到相關資訊"

_ASK_PROMPT = f"""你是「會議助手」的問答模組。根據以下歷史會議紀錄片段回答使用者的問題。

規則：
1. 只根據提供的片段回答；找不到答案就直說「{NOT_FOUND_ANSWER}」，禁止編造。
2. 用繁體中文簡潔回答（一般 3 句以內；問「哪些會議」時逐場列出）；人名與專有名詞保留原文寫法。
3. 提到具體事實時，註明出自哪場會議（標題與日期）。
4. 只輸出 JSON：{{{{"answer": "回答", "cited": [回答實際用到的片段編號]}}}}

會議紀錄片段（每段開頭的 [數字] 是片段編號）：
---
{{context}}
---

問題：{{question}}"""

# 每次提問送進回答提示的片段上限：語意檢索取前 8、關鍵字補前 4，合併去重後最多這麼多
SEMANTIC_K, KEYWORD_K, MAX_PASSAGES = 8, 4, 10


def parse_answer(raw: str | None) -> tuple[str, list[int]]:
    """回答與引用的片段編號。模型沒照格式回（純文字）時整段當回答、沒有引用。"""
    text = (raw or "").strip()
    try:
        data = json.loads(strip_code_fence(text))
    except ValueError:
        return text, []
    if not isinstance(data, dict) or not isinstance(data.get("answer"), str):
        return text, []
    cited = [n for n in data.get("cited") or [] if isinstance(n, int) and not isinstance(n, bool)]
    return data["answer"].strip(), cited


def _conditions(plan: AskPlan) -> dict | None:
    if not plan.has_filters:
        return None
    return {
        "date_from": plan.date_from.isoformat() if plan.date_from else None,
        "date_to": plan.date_to.isoformat() if plan.date_to else None,
        "kinds": plan.kinds,
        "people": plan.people,
    }


class AskAgent:
    def __init__(
        self,
        index: RagIndex,
        store,
        api_key=None,
        api_keys=None,
        on_call=None,
        # 與 Settings.gemini_model 的預設同一顆：高額度、不會飄版本（main.py 仍以設定值覆蓋）
        model: str = "gemini-3.5-flash-lite",
        generate=None,
        kinds: list[str] | None = None,
        today=today_local,
    ):
        self._index = index
        self._store = store
        self._pool = KeyPool(api_keys if api_keys else [api_key])
        # 每打一次 API 就回報一次，用量統計才會算到重試與換金鑰
        self._on_call = on_call
        self.model = model
        self._generate = generate or self._generate_with_gemini
        self._kinds = list(kinds or [])
        self._today = today

    def _plan(self, question: str) -> AskPlan:
        # 解析條件是加分項：額度、網路、格式任何失敗都退回「不套條件」，不讓提問跟著失敗
        try:
            raw = self._generate(build_plan_prompt(question, self._today(), self._kinds))
        except Exception as exc:
            logger.warning("問答條件解析失敗，改用原問題檢索：%s", exc)
            return AskPlan(query=question)
        return parse_plan(raw, question, self._kinds)

    def ask(
        self,
        question: str,
        meeting_ids: list[str] | None = None,
        user: str = DEFAULT_USER,
    ) -> dict:
        question = question.strip()
        if not question:
            raise ValueError("問題不可為空")
        self._index.sync(self._store, user=user)
        meetings = self._store.list_meetings(user=user, include_transcript=True)
        if meeting_ids is not None:
            allowed = set(meeting_ids)
            meetings = [m for m in meetings if m["id"] in allowed]
        if not meetings:
            message = (
                "所選會議中沒有可檢索的內容，換個範圍或先分析一場會議吧。"
                if meeting_ids is not None
                else "目前還沒有任何會議紀錄可供查詢，先分析一場會議吧。"
            )
            return {"answer": message, "sources": [], "passages": [], "conditions": None}

        plan = self._plan(question)
        conditions = _conditions(plan)
        ids = [m["id"] for m in meetings]
        if plan.has_filters:
            ids = filter_meetings(meetings, self._store.list_tasks(user=user), plan)
            if not ids:
                # 不放寬條件去別的會議硬找：答案會來自使用者沒問的會議
                return {"answer": "沒有符合條件的會議，換個說法或放寬時間、種類、人名再問一次。",
                        "sources": [], "passages": [], "conditions": conditions}

        hits, seen = [], set()
        for h in (self._index.search(plan.query, k=SEMANTIC_K, meeting_ids=ids, user=user)
                  + self._index.keyword_search(plan.keywords, k=KEYWORD_K, meeting_ids=ids, user=user)):
            key = (h["meeting_id"], h["text"])
            if key not in seen:
                seen.add(key)
                hits.append(h)
        hits = hits[:MAX_PASSAGES]
        if not hits:
            return {"answer": "這些會議中沒有可檢索的內容。", "sources": [], "passages": [],
                    "conditions": conditions}

        context = "\n\n".join(
            f"[{i}]【{h['title']}｜{h['date']}{'｜' + h['time'] if h['time'] else ''}】\n{h['text']}"
            for i, h in enumerate(hits, 1)
        )
        answer, cited_nos = parse_answer(
            self._generate(_ASK_PROMPT.format(context=context, question=question))
        )

        # 檢索一定會回傳最接近的幾筆（再不相干也有分數），所以「有沒有命中」不能
        # 當判準。答不出來時列來源與段落，等於指著那幾場會議說答案出自那裡
        if NOT_FOUND_ANSWER in answer:
            return {"answer": answer, "sources": [], "passages": [], "conditions": conditions}

        cited = {n - 1 for n in cited_nos if 1 <= n <= len(hits)}
        passages = []
        for i, h in enumerate(hits):
            time, quote = (
                focus_line(h["text"], plan.keywords, answer) if h["source"] == "transcript" else (None, "")
            )
            passages.append({
                **{k: h[k] for k in ("meeting_id", "title", "date", "source", "text")},
                "time": time, "quote": quote, "cited": i in cited,
            })
        passages.sort(key=lambda p: not p["cited"])  # 穩定排序：引用的排前面，其餘維持檢索順序

        # 來源＝回答實際引用的會議；模型沒標引用時退回列出所有命中的會議
        sources, seen_ids = [], set()
        for p in passages:
            if (cited and not p["cited"]) or p["meeting_id"] in seen_ids:
                continue
            seen_ids.add(p["meeting_id"])
            sources.append({"meeting_id": p["meeting_id"], "title": p["title"], "date": p["date"]})
        return {"answer": answer, "sources": sources, "passages": passages, "conditions": conditions}

    def _generate_with_gemini(self, prompt: str) -> str:
        if not self._pool:
            raise RagError("未設定 GEMINI_API_KEY：跨會議問答需要 Gemini 金鑰")
        # 過載時自動改用另一個模型（見 call_with_model_fallback）
        return call_with_model_fallback(
            self._pool,
            self.model,
            lambda key, model: self._call_gemini(key, prompt, model),
            on_call=self._on_call,
        )

    def _call_gemini(self, key: str, prompt: str, model: str | None = None) -> str:
        from google import genai

        client = genai.Client(api_key=key)
        response = client.models.generate_content(
            model=model or self.model, contents=prompt, config={"temperature": 0.2}
        )
        return response.text or ""
