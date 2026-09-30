"""RAG 跨會議問答測試：假 Embedder ＋假 generate，不觸網。

把歷史會議（逐字稿＋摘要卡）切塊向量化，問問題時檢索最相關片段，
交給 Gemini 依片段回答並附上來源會議。
"""
import pytest

from app.models import MeetingAnalysis
from app.rag import AskAgent, RagIndex, chunk_transcript, cosine, focus_line
from app.stores.local_store import LocalJsonStore
from tests.test_models import make_valid_payload
from tests.test_stores import make_analysis


def rag_store(tmp_path):
    """索引的持久化後端。用獨立的 db 檔，同一個 tmp_path 重複建構會共用同一份
    索引——測「重新載入」時就是在模擬重啟服務。"""
    return LocalJsonStore(tmp_path / "ragdb.json")


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


# ---- chunk_transcript：依行切塊，記下時間戳才跳得回逐字稿 ----

def test_short_transcript_single_chunk_with_time():
    assert chunk_transcript("[0:05] 講者A：短文字") == [{"text": "[0:05] 講者A：短文字", "time": "0:05"}]


def test_empty_transcript_no_chunks():
    assert chunk_transcript("   \n  ") == []


def test_chunks_never_cut_a_line_and_overlap_by_one_line():
    lines = [f"[{i}:00] 講者A：第{i}句" + "話" * 30 for i in range(40)]
    chunks = chunk_transcript("\n".join(lines), size=200)
    assert len(chunks) > 1
    for c in chunks:
        assert all(line in lines for line in c["text"].split("\n"))  # 沒有被切斷的行
        assert c["time"] == c["text"][1:c["text"].index("]")]  # 第一行的時間
    assert chunks[1]["text"].split("\n")[0] == chunks[0]["text"].split("\n")[-1]
    assert chunks[-1]["text"].endswith(lines[-1])


def test_chunk_time_uses_first_timestamped_line():
    text = "講者A：沒有時間的開場\n[1:02] 講者B：這行有"
    assert chunk_transcript(text)[0]["time"] == "1:02"
    assert chunk_transcript("沒有任何時間戳")[0]["time"] is None


def test_overlong_single_line_is_split():
    chunks = chunk_transcript("[0:01] " + "字" * 900, size=400)
    assert len(chunks) == 3
    assert all(len(c["text"]) <= 400 for c in chunks)
    assert all(c["time"] == "0:01" for c in chunks)


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
    result = pipeline.process_transcript("志明下週一交 prompt")
    assert "志明下週一交 prompt" in store.get_meeting(result["meeting_id"])["transcript"]


# ---- RagIndex ----

def make_store_with_meeting(tmp_path, transcript="Kevin 說 API 由小明負責，週五前完成。"):
    store = LocalJsonStore(tmp_path / "db.json")
    store.save_meeting(make_analysis(), transcript=transcript)
    return store


def test_sync_indexes_new_meetings_and_search_finds_relevant(tmp_path):
    store = make_store_with_meeting(tmp_path)
    index = RagIndex(rag_store(tmp_path), embedder=FakeEmbedder())
    assert index.sync(store) > 0

    hits = index.search("API 誰負責？", k=2)
    assert hits
    assert any("API" in h["text"] for h in hits)
    assert hits[0]["title"] == "專題進度會議"


def test_sync_is_incremental(tmp_path):
    store = make_store_with_meeting(tmp_path)
    emb = FakeEmbedder()
    index = RagIndex(rag_store(tmp_path), embedder=emb)
    index.sync(store)
    calls_after_first = emb.calls
    assert index.sync(store) == 0  # 沒有新會議 → 不重新向量化
    assert emb.calls == calls_after_first


def test_search_can_scope_to_selected_meetings(tmp_path):
    """詢問會議可複選範圍：檢索只在所選會議內進行。"""
    store = LocalJsonStore(tmp_path / "db.json")
    id1 = store.save_meeting(make_analysis(), transcript="Kevin：API 由小明負責。")
    id2 = store.save_meeting(make_analysis(), transcript="Amy：資料庫下週遷移。")
    index = RagIndex(rag_store(tmp_path), embedder=FakeEmbedder())
    index.sync(store)

    hits = index.search("API 誰負責？", k=10, meeting_ids=[id2])
    assert hits
    assert all(h["meeting_id"] == id2 for h in hits)
    assert index.search("API", k=10, meeting_ids=[]) == []  # 空範圍 = 無結果
    # 不給範圍 → 全部會議
    assert {h["meeting_id"] for h in index.search("API", k=10)} == {id1, id2}


def test_drop_meeting_invalidates_index_and_resync_reembeds(tmp_path):
    """會議被編輯/刪除後索引要作廢，下次 sync 用新內容重建，問答才不會回舊資料。"""
    store = make_store_with_meeting(tmp_path)
    meeting_id = store.list_meetings()[0]["id"]
    index = RagIndex(rag_store(tmp_path), embedder=FakeEmbedder())
    index.sync(store)

    assert index.drop_meeting(meeting_id) > 0
    assert index.search("API", k=5) == []  # 索引已清空
    assert index.drop_meeting(meeting_id) == 0  # 再刪沒東西

    # 會議還在 store（編輯情境）→ 下次 sync 重新向量化
    assert index.sync(store) > 0
    assert index.search("API", k=1)

    # 作廢要落地：重新載入索引檔也不能殘留
    index.drop_meeting(meeting_id)
    reloaded = RagIndex(rag_store(tmp_path), embedder=FakeEmbedder())
    assert reloaded.search("API", k=5) == []


def test_index_persists_to_disk(tmp_path):
    store = make_store_with_meeting(tmp_path)
    emb = FakeEmbedder()
    RagIndex(rag_store(tmp_path), embedder=emb).sync(store)

    emb2 = FakeEmbedder()
    index2 = RagIndex(rag_store(tmp_path), embedder=emb2)
    assert index2.sync(store) == 0  # 從磁碟載入，不重算
    assert index2.search("API", k=1)  # 查詢會 embed 問題本身
    assert emb2.calls == 1


def test_index_wiped_when_embedding_dim_changes(tmp_path):
    """向量維度改過（例如 3072→768）時，舊索引要作廢，避免與新問題向量不同長。"""
    store = make_store_with_meeting(tmp_path)

    class Emb768(FakeEmbedder):
        dim = 768

    class Emb1536(FakeEmbedder):
        dim = 1536

    RagIndex(rag_store(tmp_path), embedder=Emb768()).sync(store)

    # 用不同維度的 embedder 載入 → 舊索引視為失效（清空）
    reloaded = RagIndex(rag_store(tmp_path), embedder=Emb1536())
    assert reloaded.search("API", k=5) == []

    # 同維度載入 → 仍保留（載入不會覆寫，檔案還是 768 維）
    same = RagIndex(rag_store(tmp_path), embedder=Emb768())
    assert same.search("API", k=1)


def test_reset_clears_index(tmp_path):
    store = make_store_with_meeting(tmp_path)
    index = RagIndex(rag_store(tmp_path), embedder=FakeEmbedder())
    index.sync(store)
    assert index.search("API", k=1)
    index.reset()
    assert index.search("API", k=5) == []
    # 落地：重新載入也空
    assert RagIndex(rag_store(tmp_path), embedder=FakeEmbedder()).search("API", k=5) == []


def test_records_carry_source_and_time(tmp_path):
    store = make_store_with_meeting(tmp_path, transcript="[0:12] Kevin：API 由小明負責。")
    index = RagIndex(rag_store(tmp_path), embedder=FakeEmbedder())
    index.sync(store)
    hits = {h["source"]: h for h in index.search("API", k=5)}
    assert hits["transcript"]["time"] == "0:12"
    assert hits["summary"]["time"] is None


def test_index_without_source_field_is_rebuilt(tmp_path):
    """改版前的逐字稿切塊是固定 400 字、沒有時間戳，跳不回原文，整份作廢重建。"""
    emb = FakeEmbedder()
    store = rag_store(tmp_path)
    store.save_rag_records(None, [{"meeting_id": "old1", "user": "default", "title": "舊",
                                   "date": "", "text": "API", "vector": emb.embed(["API"])[0]}])
    assert RagIndex(store, embedder=emb).search("API", k=5) == []


def test_keyword_search_finds_exact_terms_case_insensitively(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    id1 = store.save_meeting(make_analysis(), transcript="[0:01] Kevin：預算要砍 Demo 費用")
    id2 = store.save_meeting(make_analysis(), transcript="[0:01] Amy：資料庫下週遷移")
    index = RagIndex(rag_store(tmp_path), embedder=FakeEmbedder())
    index.sync(store)
    hits = index.keyword_search(["demo", "預算"], k=10)
    assert {h["meeting_id"] for h in hits} == {id1}
    assert hits[0]["time"] == "0:01"
    assert index.keyword_search(["預算"], k=10, meeting_ids=[id2]) == []
    assert index.keyword_search([], k=10) == []


def test_summary_card_indexed_even_without_transcript(tmp_path):
    """舊會議沒存逐字稿，至少摘要/決議/代辦要可被檢索。"""
    store = LocalJsonStore(tmp_path / "db.json")
    store.save_meeting(make_analysis())  # 沒有 transcript
    index = RagIndex(rag_store(tmp_path), embedder=FakeEmbedder())
    assert index.sync(store) > 0
    hits = index.search("介面", k=2)
    assert any("要不要支援英文介面" in h["text"] for h in hits)


def test_summary_card_handles_missing_summary(tmp_path):
    """summary 功能沒被使用（None）時，索引仍要能建立，且不能把 Python 的
    None 字面值當成摘要文字存進去。"""
    payload = make_valid_payload()
    payload["meeting"]["summary"] = None
    store = LocalJsonStore(tmp_path / "db.json")
    store.save_meeting(MeetingAnalysis.model_validate(payload))
    index = RagIndex(rag_store(tmp_path), embedder=FakeEmbedder())
    assert index.sync(store) > 0
    hits = index.search("介面", k=5)
    assert all("None" not in h["text"] for h in hits)


# ---- AskAgent ----

def test_ask_agent_answers_with_retrieved_context_and_sources(tmp_path):
    store = make_store_with_meeting(tmp_path)
    index = RagIndex(rag_store(tmp_path), embedder=FakeEmbedder())
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


def test_answer_that_found_nothing_lists_no_sources(tmp_path):
    """答案說「找不到」時還掛一排來源，等於指著兩場會議說答案出自那裡。

    實測問「有沒有提到股價」，回的是「在現有的會議紀錄中找不到相關資訊」，下面
    卻列了兩場會議。來源欄的意思是「這個答案根據這些會議」，沒有答案就沒有來源。
    向量檢索一定會回傳最接近的幾筆（再不相干也有分數），所以不能拿「有沒有命中」
    當判準，要看模型最後有沒有答出東西——而那句話是 _ASK_PROMPT 規定的固定用語。
    """
    store = make_store_with_meeting(tmp_path)
    index = RagIndex(rag_store(tmp_path), embedder=FakeEmbedder())

    agent = AskAgent(
        index=index,
        store=store,
        generate=lambda p: "在現有的會議紀錄中找不到相關資訊。",
    )
    assert agent.ask("這次會議的股價是多少？")["sources"] == []


def test_ask_agent_empty_store_answers_without_llm(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    index = RagIndex(rag_store(tmp_path), embedder=FakeEmbedder())

    def boom(prompt):
        raise AssertionError("沒有資料不該呼叫 LLM")

    agent = AskAgent(index=index, store=store, generate=boom)
    result = agent.ask("上次開會說什麼？")
    assert "沒有" in result["answer"]
    assert result["sources"] == []


# ---- 多租戶隔離 ----
# store 層每筆讀寫都帶 user，但 RAG 索引原本完全沒有 user 概念：sync 呼叫
# store 時不帶 user、記錄裡也不存 user。接上真正的登入之後，A 問問題會檢索到
# B 的會議內容——這是整條資料流唯一一個「加登入時不會自動安全」的破口。

def make_two_user_store(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    mine = store.save_meeting(
        make_analysis(), transcript="Kevin：API 由小明負責。", user="me"
    )
    theirs = store.save_meeting(
        make_analysis(), transcript="Amy：資料庫下週遷移。", user="other"
    )
    return store, mine, theirs


def test_sync_indexes_only_the_requesting_user(tmp_path):
    store, mine, theirs = make_two_user_store(tmp_path)
    index = RagIndex(rag_store(tmp_path), embedder=FakeEmbedder())

    index.sync(store, user="me")

    found = {h["meeting_id"] for h in index.search("API 資料庫", k=50, user="me")}
    assert found == {mine}
    assert theirs not in found


def test_search_never_returns_another_users_records(tmp_path):
    store, mine, theirs = make_two_user_store(tmp_path)
    index = RagIndex(rag_store(tmp_path), embedder=FakeEmbedder())
    index.sync(store, user="me")
    index.sync(store, user="other")

    # 兩人的向量存在同一份索引檔裡，但彼此看不到對方的
    assert all(h["meeting_id"] == mine for h in index.search("資料庫", k=50, user="me"))
    assert all(
        h["meeting_id"] == theirs for h in index.search("API", k=50, user="other")
    )
    # 連指名對方的 meeting_id 也檢索不到
    assert index.search("資料庫", k=50, meeting_ids=[theirs], user="me") == []


def test_ask_agent_scopes_retrieval_to_the_user(tmp_path):
    store, mine, theirs = make_two_user_store(tmp_path)
    index = RagIndex(rag_store(tmp_path), embedder=FakeEmbedder())
    captured = {}

    def fake_generate(prompt):
        captured["prompt"] = prompt
        return "答案"

    agent = AskAgent(index=index, store=store, generate=fake_generate)
    result = agent.ask("資料庫什麼時候遷移？", user="me")

    assert "資料庫下週遷移" not in captured.get("prompt", "")
    assert all(s["meeting_id"] == mine for s in result["sources"])


def test_legacy_index_records_belong_to_default_user(tmp_path):
    """改版前存下來的索引記錄沒有 user 欄位，要視為 DEFAULT_USER 的，
    不能因為欄位不存在就整份查不到（等同無聲失效）。"""
    from app.stores.base import DEFAULT_USER

    emb = FakeEmbedder()
    store = rag_store(tmp_path)
    store.save_rag_records(None, [{
        "meeting_id": "old1",
        "title": "舊會議",
        "date": "2026-01-01",
        "text": "API 由小明負責",
        "source": "transcript",
        "time": None,
        "vector": emb.embed(["API 由小明負責"])[0],
    }])
    index = RagIndex(store, embedder=emb)

    assert index.search("API", k=5, user=DEFAULT_USER)
    assert index.search("API", k=5, user="someone-else") == []


def test_reset_can_clear_only_one_user(tmp_path):
    store, mine, theirs = make_two_user_store(tmp_path)
    index = RagIndex(rag_store(tmp_path), embedder=FakeEmbedder())
    index.sync(store, user="me")
    index.sync(store, user="other")

    index.reset(user="me")  # 還原備份是單一使用者的事，不該炸掉別人的索引

    assert index.search("API", k=50, user="me") == []
    assert index.search("資料庫", k=50, user="other")


def test_ask_agent_default_model_is_not_a_drifting_alias(tmp_path):
    """任何 -latest 都會飄到「當下最新版」，而剛發布的版本正在被全世界搶，
    實測會回 503（README 明講別用）。main.py 會用設定值覆蓋，但直接建 AskAgent
    的人不該一不小心就踩到。

    原本只查 endswith("-flash-latest")，"gemini-flash-lite-latest" 結尾是
    "-flash-lite-latest" 就從旁邊溜過去了——守的是字串不是那條規則。
    """
    store = LocalJsonStore(tmp_path / "db.json")
    agent = AskAgent(index=RagIndex(store, embedder=None), store=store)
    assert not agent.model.endswith("-latest")


# ---- 詢問會議 v2：先解析條件，再檢索、回答並標出引用 ----

def dated_analysis(title, day, attendees=()):
    payload = make_valid_payload()
    payload["meeting"].update(title=title, date=day, attendees=list(attendees))
    return MeetingAnalysis.model_validate(payload)


class ScriptedLLM:
    """依 prompt 是哪一步回不同內容，並記下每次呼叫。"""

    def __init__(self, plan, answer):
        self.plan, self.answer, self.prompts = plan, answer, []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        is_plan = "檢索規劃器" in prompt
        reply = self.plan if is_plan else self.answer
        if isinstance(reply, Exception):
            raise reply
        return reply


def two_month_store(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    aug = store.save_meeting(dated_analysis("八月客戶會", "2026-08-12"), kind="銷售拜訪",
                             transcript="[0:10] Kevin：API 報價八月底前給")
    sep = store.save_meeting(dated_analysis("九月週會", "2026-09-03"),
                             transcript="[0:20] Amy：API 預算下週定")
    return store, aug, sep


def v2_agent(tmp_path, store, llm):
    index = RagIndex(rag_store(tmp_path), embedder=FakeEmbedder())
    return AskAgent(index=index, store=store, generate=llm, kinds=["一般會議", "銷售拜訪"])


def test_plan_conditions_narrow_the_meetings_searched(tmp_path):
    store, aug, sep = two_month_store(tmp_path)
    llm = ScriptedLLM(
        plan='{"date_from": "2026-08-01", "date_to": "2026-08-31", "kinds": ["銷售拜訪"], "query": "API 報價"}',
        answer='{"answer": "八月底前給報價。", "cited": [1]}',
    )
    result = v2_agent(tmp_path, store, llm).ask("上個月客戶會議 API 報價什麼時候給？")

    assert len(llm.prompts) == 2
    assert "API 報價八月底前給" in llm.prompts[1]
    assert "API 預算下週定" not in llm.prompts[1]  # 九月的會議被條件擋掉
    assert result["answer"] == "八月底前給報價。"
    assert result["conditions"] == {"date_from": "2026-08-01", "date_to": "2026-08-31",
                                    "kinds": ["銷售拜訪"], "people": []}
    assert {s["meeting_id"] for s in result["sources"]} == {aug}


def test_no_meeting_matches_the_conditions_skips_the_second_call(tmp_path):
    store, _, _ = two_month_store(tmp_path)
    llm = ScriptedLLM(plan='{"date_from": "2025-01-01", "date_to": "2025-01-31", "query": "API"}',
                      answer=AssertionError("篩不到會議就不該再問模型"))
    result = v2_agent(tmp_path, store, llm).ask("去年一月的會議談了什麼 API？")
    assert "沒有符合條件的會議" in result["answer"]
    assert result["sources"] == [] and result["passages"] == []
    assert result["conditions"]["date_from"] == "2025-01-01"
    assert len(llm.prompts) == 1


def test_manual_scope_is_intersected_with_the_plan(tmp_path):
    store, aug, sep = two_month_store(tmp_path)
    llm = ScriptedLLM(plan='{"kinds": ["銷售拜訪"], "query": "API"}', answer="x")
    result = v2_agent(tmp_path, store, llm).ask("客戶會議的 API？", meeting_ids=[sep])
    assert "沒有符合條件的會議" in result["answer"]


def test_plan_failure_falls_back_to_plain_search(tmp_path):
    store, aug, sep = two_month_store(tmp_path)
    llm = ScriptedLLM(plan=RuntimeError("429 quota"), answer="API 報價八月底前給。")
    result = v2_agent(tmp_path, store, llm).ask("API 報價什麼時候給？")
    assert result["answer"] == "API 報價八月底前給。"
    assert result["conditions"] is None
    assert {s["meeting_id"] for s in result["sources"]} == {aug, sep}  # 沒標引用 → 列全部命中


def test_cited_passages_come_first_with_time_and_limit_sources(tmp_path):
    store, aug, sep = two_month_store(tmp_path)
    llm = ScriptedLLM(plan='{"query": "API"}', answer=None)
    agent = v2_agent(tmp_path, store, llm)
    # 先看片段編號再決定要引用哪一個：找出九月那段逐字稿的編號
    llm.answer = '{"answer": "預算下週定。", "cited": []}'
    agent.ask("API？")
    numbered = [line for line in llm.prompts[-1].splitlines() if "API 預算下週定" in line or "【" in line]
    sep_no = next(int(line[1:line.index("]")]) for line in numbered
                  if line.startswith("[") and "0:20" in line)
    llm.answer = f'{{"answer": "預算下週定。", "cited": [{sep_no}]}}'

    result = agent.ask("API？")
    first = result["passages"][0]
    assert first["cited"] and first["meeting_id"] == sep and first["time"] == "0:20"
    assert "API 預算下週定" in first["text"]
    assert [s["meeting_id"] for s in result["sources"]] == [sep]


def test_keyword_hits_reach_the_prompt_even_when_embeddings_miss(tmp_path):
    """FakeEmbedder 不認得「報價」這個詞，只有關鍵字檢索找得到。"""
    store = LocalJsonStore(tmp_path / "db.json")
    store.save_meeting(make_analysis(), transcript="[0:01] Kevin：報價單週五寄出")
    for i in range(10):  # 塞滿語意檢索的名額
        store.save_meeting(make_analysis(), transcript=f"[0:0{i}] Amy：API 介面 demo 第{i}版")
    llm = ScriptedLLM(plan='{"query": "API 介面", "keywords": ["報價單"]}', answer="週五寄出。")
    v2_agent(tmp_path, store, llm).ask("報價單什麼時候寄？")
    assert "報價單週五寄出" in llm.prompts[1]


def test_not_found_answer_lists_no_passages(tmp_path):
    store, _, _ = two_month_store(tmp_path)
    llm = ScriptedLLM(plan='{"query": "股價"}',
                      answer='{"answer": "在現有的會議紀錄中找不到相關資訊", "cited": [1]}')
    result = v2_agent(tmp_path, store, llm).ask("股價多少？")
    assert result["sources"] == [] and result["passages"] == []


# ---- 焦點行：一個片段好幾行，跳轉要落在真正回答問題的那一行 ----

PASSAGE = "[0:05] 王小明：今天主要談導入時程\n[1:20] 講者B：預算上限是八十萬\n[2:40] 王小明：報價單我們九月五號前寄出"


def test_focus_line_prefers_keyword_hits():
    assert focus_line(PASSAGE, ["預算"], "") == ("1:20", "講者B：預算上限是八十萬")


def test_focus_line_falls_back_to_answer_wording():
    assert focus_line(PASSAGE, [], "報價單會在九月五號前寄出")[0] == "2:40"


def test_focus_line_without_any_signal_is_the_first_line():
    assert focus_line(PASSAGE, [], "") == ("0:05", "王小明：今天主要談導入時程")


def test_focus_line_inherits_the_last_seen_time():
    assert focus_line("[0:05] 講者A：開場\n接著談預算", ["預算"], "") == ("0:05", "接著談預算")


def test_passages_point_at_their_focus_line(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    store.save_meeting(make_analysis(), transcript=PASSAGE)
    llm = ScriptedLLM(plan='{"query": "報價", "keywords": ["報價單"]}',
                      answer='{"answer": "九月五號前寄出。", "cited": [1]}')
    result = v2_agent(tmp_path, store, llm).ask("報價單什麼時候寄？")
    p = next(p for p in result["passages"] if p["source"] == "transcript")
    assert (p["time"], p["quote"]) == ("2:40", "王小明：報價單我們九月五號前寄出")
