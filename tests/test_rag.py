"""RAG 跨會議問答測試：假 Embedder ＋假 generate，不觸網。

把歷史會議（逐字稿＋摘要卡）切塊向量化，問問題時檢索最相關片段，
交給 Gemini 依片段回答並附上來源會議。
"""
from pathlib import Path

import pytest

from app.rag import AskAgent, RagIndex, chunk_text, cosine
from app.stores.local_store import LocalJsonStore
from tests.test_stores import make_analysis


class FakeEmbedder:
    """關鍵字計數向量：同字多次出現 → 相似度高，決定性且不觸網。"""

    KEYWORDS = ["API", "介面", "demo", "資料庫"]

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [
            [float(t.count(k)) for k in self.KEYWORDS] + [1.0] for t in texts
        ]


# ---- chunk_text ----

def test_short_text_single_chunk():
    assert chunk_text("短文字") == ["短文字"]


def test_empty_text_no_chunks():
    assert chunk_text("   ") == []


def test_long_text_chunks_cover_everything_with_overlap():
    text = "月".join(str(i) for i in range(500))  # 約 1800 字
    chunks = chunk_text(text, size=400, overlap=80)
    assert all(len(c) <= 400 for c in chunks)
    assert chunks[0] == text[:400]
    # 相鄰塊有重疊：後一塊的開頭在前一塊裡出現過
    assert chunks[1][:80] in chunks[0]
    # 拼回去要涵蓋原文結尾
    assert text[-100:] in chunks[-1] + chunks[-2]


# ---- cosine ----

def test_cosine_identical_and_orthogonal():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


# ---- 逐字稿儲存 ----

def test_store_saves_transcript_and_strips_from_list(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    meeting_id = store.save_meeting(make_analysis(), transcript="Kevin：API 小明負責。")
    assert store.get_meeting(meeting_id)["transcript"] == "Kevin：API 小明負責。"
    # 列表回應保持輕量，不含逐字稿全文
    assert "transcript" not in store.list_meetings()[0]


def test_orchestrator_passes_transcript_to_store(tmp_path):
    from app.agents.decision_agent import DecisionAgent
    from app.agents.executor_agent import ExecutorAgent
    from app.agents.notifier_agent import NotifierAgent
    from app.agents.parser_agent import ParserAgent
    from app.orchestrator import Orchestrator
    from tests.test_decision import valid_json

    store = LocalJsonStore(tmp_path / "db.json")
    pipeline = Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(generate=lambda p: valid_json()),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(tmp_path / "n"),
    )
    result = pipeline.process_transcript("鈺翔下週一交 prompt")
    assert "鈺翔下週一交 prompt" in store.get_meeting(result["meeting_id"])["transcript"]


# ---- RagIndex ----

def make_store_with_meeting(tmp_path, transcript="Kevin 說 API 由小明負責，週五前完成。"):
    store = LocalJsonStore(tmp_path / "db.json")
    store.save_meeting(make_analysis(), transcript=transcript)
    return store


def test_sync_indexes_new_meetings_and_search_finds_relevant(tmp_path):
    store = make_store_with_meeting(tmp_path)
    index = RagIndex(tmp_path / "rag.json", embedder=FakeEmbedder())
    assert index.sync(store) > 0

    hits = index.search("API 誰負責？", k=2)
    assert hits
    assert any("API" in h["text"] for h in hits)
    assert hits[0]["title"] == "專題進度會議"


def test_sync_is_incremental(tmp_path):
    store = make_store_with_meeting(tmp_path)
    emb = FakeEmbedder()
    index = RagIndex(tmp_path / "rag.json", embedder=emb)
    index.sync(store)
    calls_after_first = emb.calls
    assert index.sync(store) == 0  # 沒有新會議 → 不重新向量化
    assert emb.calls == calls_after_first


def test_index_persists_to_disk(tmp_path):
    store = make_store_with_meeting(tmp_path)
    emb = FakeEmbedder()
    RagIndex(tmp_path / "rag.json", embedder=emb).sync(store)

    emb2 = FakeEmbedder()
    index2 = RagIndex(tmp_path / "rag.json", embedder=emb2)
    assert index2.sync(store) == 0  # 從磁碟載入，不重算
    assert index2.search("API", k=1)  # 查詢會 embed 問題本身
    assert emb2.calls == 1


def test_summary_card_indexed_even_without_transcript(tmp_path):
    """舊會議沒存逐字稿，至少摘要/決議/代辦要可被檢索。"""
    store = LocalJsonStore(tmp_path / "db.json")
    store.save_meeting(make_analysis())  # 沒有 transcript
    index = RagIndex(tmp_path / "rag.json", embedder=FakeEmbedder())
    assert index.sync(store) > 0
    hits = index.search("介面", k=2)
    assert any("要不要支援英文介面" in h["text"] for h in hits)


# ---- AskAgent ----

def test_ask_agent_answers_with_retrieved_context_and_sources(tmp_path):
    store = make_store_with_meeting(tmp_path)
    index = RagIndex(tmp_path / "rag.json", embedder=FakeEmbedder())
    captured = {}

    def fake_generate(prompt):
        captured["prompt"] = prompt
        return "API 由小明負責，週五前完成。"

    agent = AskAgent(index=index, store=store, generate=fake_generate)
    result = agent.ask("API 誰負責？")

    assert result["answer"] == "API 由小明負責，週五前完成。"
    assert "API 由小明負責" in captured["prompt"]  # 檢索到的片段要進 prompt
    assert "API 誰負責？" in captured["prompt"]
    assert result["sources"][0]["title"] == "專題進度會議"


def test_ask_agent_empty_store_answers_without_llm(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    index = RagIndex(tmp_path / "rag.json", embedder=FakeEmbedder())

    def boom(prompt):
        raise AssertionError("沒有資料不該呼叫 LLM")

    agent = AskAgent(index=index, store=store, generate=boom)
    result = agent.ask("上次開會說什麼？")
    assert "沒有" in result["answer"]
    assert result["sources"] == []
