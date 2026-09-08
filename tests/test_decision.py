"""Decision Agent 測試：以注入的假 generate 函式取代真實 Gemini 呼叫。"""
import json
from datetime import date

import pytest

from app.agents.decision_agent import DecisionAgent, DecisionAgentError
from tests.test_models import make_valid_payload

MEETING_DATE = date(2026, 7, 12)


def valid_json() -> str:
    return json.dumps(make_valid_payload(), ensure_ascii=False)


def test_valid_response_parses_to_analysis():
    agent = DecisionAgent(generate=lambda prompt: valid_json())
    analysis = agent.analyze("鈺翔下週一前把 prompt 寫好", meeting_date=MEETING_DATE)
    assert analysis.meeting.title == "專題進度會議"
    assert analysis.todos[0].owner == "王鈺翔"


def test_markdown_code_fence_stripped():
    fenced = "```json\n" + valid_json() + "\n```"
    agent = DecisionAgent(generate=lambda prompt: fenced)
    analysis = agent.analyze("測試", meeting_date=MEETING_DATE)
    assert analysis.meeting.title == "專題進度會議"


def test_prompt_contains_meeting_date_and_transcript():
    captured = {}

    def fake_generate(prompt):
        captured["prompt"] = prompt
        return valid_json()

    DecisionAgent(generate=fake_generate).analyze(
        "Kevin 說 demo 排週五", meeting_date=MEETING_DATE
    )
    assert "2026-07-12" in captured["prompt"]
    assert "Kevin 說 demo 排週五" in captured["prompt"]


def test_retries_on_invalid_json_with_error_feedback():
    calls = []

    def flaky_generate(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return "抱歉，我整理如下：不是 JSON"
        return valid_json()

    agent = DecisionAgent(generate=flaky_generate)
    analysis = agent.analyze("測試", meeting_date=MEETING_DATE)
    assert analysis.meeting.title == "專題進度會議"
    assert len(calls) == 2
    # 重試的 prompt 應該帶上錯誤回饋
    assert "上一次的輸出無法解析" in calls[1]


def test_gives_up_after_max_attempts():
    agent = DecisionAgent(generate=lambda prompt: "永遠不是 JSON", max_attempts=3)
    with pytest.raises(DecisionAgentError):
        agent.analyze("測試", meeting_date=MEETING_DATE)


def test_missing_api_key_raises_clear_error():
    agent = DecisionAgent(api_key=None)  # 未注入 generate → 走真實路徑
    with pytest.raises(DecisionAgentError, match="GEMINI_API_KEY"):
        agent.analyze("測試", meeting_date=MEETING_DATE)


def test_prompt_instructs_dedupe_and_priority_reason():
    """prompt 必須要求：重複任務合併、優先級附理由。"""
    from app.agents.decision_agent import build_prompt

    prompt = build_prompt("測試", MEETING_DATE)
    assert "合併" in prompt  # 同一件事講多次要合併成一筆
    assert "priority_reason" in prompt


def test_prompt_includes_kind_hint_when_given():
    from app.agents.decision_agent import build_prompt

    # 舊值「講座」會被對應到還存在的種類
    prompt = build_prompt("測試", MEETING_DATE, kind="講座")
    assert "會議種類：其它" in prompt
    # 沒指定種類時不出現種類段落（維持通用行為）
    assert "會議種類" not in build_prompt("測試", MEETING_DATE)


def test_analyze_passes_kind_into_prompt():
    captured = {}

    def fake_generate(prompt):
        captured["prompt"] = prompt
        return valid_json()

    DecisionAgent(generate=fake_generate).analyze(
        "測試", meeting_date=MEETING_DATE, kind="專案會議"
    )
    assert "會議種類：專案會議" in captured["prompt"]


def test_prompt_asks_for_tags():
    """schema 要包含 tags：AI 自動建議分類標籤，供歷史會議篩選。"""
    from app.agents.decision_agent import build_prompt

    prompt = build_prompt("測試", MEETING_DATE)
    assert '"tags"' in prompt


def test_prompt_includes_glossary_terms():
    from app.agents.decision_agent import build_prompt

    prompt = build_prompt(
        "測試", MEETING_DATE,
        glossary=[{"term": "王霖翔", "note": "人名"}],
    )
    assert "王霖翔（人名）" in prompt
    assert "詞彙" in prompt
    # 空詞彙表不出現詞彙段落
    assert "詞彙表" not in build_prompt("測試", MEETING_DATE)


def test_glossary_line_forbids_guessing_owner_from_vocab():
    """詞彙表只是拼字對照，不能被模型當成「猜負責人／與會者」的候選名單——
    否則詞彙表裡剛好只有一個人名時，找不到負責人的代辦事項會被誤填成他。"""
    from app.agents.decision_agent import build_prompt

    prompt = build_prompt(
        "測試", MEETING_DATE,
        glossary=[{"term": "王霖翔", "note": "人名"}],
    )
    assert "不代表" in prompt or "不能" in prompt or "禁止" in prompt
    assert "owner" in prompt.split("已知詞彙表")[1].split("。")[0]


def test_analyze_uses_injected_glossary_provider():
    captured = {}

    def fake_generate(prompt):
        captured["prompt"] = prompt
        return valid_json()

    agent = DecisionAgent(
        generate=fake_generate,
        glossary=lambda: [{"term": "TaskHub", "note": "產品名"}],
    )
    agent.analyze("測試", meeting_date=MEETING_DATE)
    assert "TaskHub（產品名）" in captured["prompt"]


def test_schema_excludes_disabled_features():
    from app.agents.decision_agent import build_prompt

    prompt = build_prompt("測試", MEETING_DATE, features={"summary"})
    assert '"summary"' in prompt
    assert '"decisions"' not in prompt
    assert '"todos"' not in prompt
    # 基本欄位（title/date/attendees/pending_items/tags）不受 features 控制，永遠存在
    assert '"attendees"' in prompt
    assert '"pending_items"' in prompt
    assert '"tags"' in prompt


def test_schema_includes_everything_when_features_not_specified():
    """向後相容：不傳 features（None）＝跟改動前一樣全部欄位都出現。"""
    from app.agents.decision_agent import build_prompt

    prompt = build_prompt("測試", MEETING_DATE)
    assert '"summary"' in prompt
    assert '"decisions"' in prompt
    assert '"todos"' in prompt


def test_analyze_forces_disabled_fields_empty_even_if_llm_ignores_instruction():
    """防禦性保護：就算 LLM 沒聽話還是生成了 summary/decisions/todos，
    features 沒開的欄位還是要被清空，不能讓停用的功能悄悄「復活」。"""
    agent = DecisionAgent(generate=lambda prompt: valid_json())
    analysis = agent.analyze(
        "測試", meeting_date=MEETING_DATE, features=set()
    )
    assert analysis.meeting.summary is None
    assert analysis.decisions == []
    assert analysis.todos == []
    # 不受控制的欄位不受影響
    assert analysis.meeting.title == "專題進度會議"
    assert analysis.pending_items


def test_analyze_keeps_enabled_fields():
    agent = DecisionAgent(generate=lambda prompt: valid_json())
    analysis = agent.analyze(
        "測試", meeting_date=MEETING_DATE, features={"summary", "decisions", "todos"}
    )
    assert analysis.meeting.summary == "討論 7 月里程碑進度與分工。"
    assert analysis.decisions
    assert analysis.todos


def test_meeting_date_defaults_to_today():
    captured = {}

    def fake_generate(prompt):
        captured["prompt"] = prompt
        return valid_json()

    DecisionAgent(generate=fake_generate).analyze("測試")
    assert str(date.today()) in captured["prompt"]


def test_schema_includes_highlights_when_enabled():
    """會議重點功能開啟時，schema 範例要包含 highlights 與時間標記說明。"""
    from app.agents.decision_agent import build_prompt

    prompt = build_prompt("測試", MEETING_DATE, features={"highlights"})
    assert '"highlights"' in prompt
    assert "時間標記" in prompt

    disabled = build_prompt("測試", MEETING_DATE, features={"summary"})
    assert '"highlights"' not in disabled
    assert "不需要" in disabled and "highlights" in disabled  # feature_note 明講不要輸出


def test_highlights_cleared_when_feature_disabled():
    """LLM 就算硬回傳 highlights，功能沒開也要被強制清空。"""
    agent = DecisionAgent(generate=lambda prompt: valid_json())
    analysis = agent.analyze(
        "測試", meeting_date=MEETING_DATE, features={"summary", "decisions", "todos"}
    )
    assert analysis.highlights == []


def test_highlights_kept_when_feature_enabled():
    agent = DecisionAgent(generate=lambda prompt: valid_json())
    analysis = agent.analyze("測試", meeting_date=MEETING_DATE, features={"highlights"})
    assert analysis.highlights[0].time == "1:02"


# ---- 會議種類（取代原本的錄音種類）----

def test_kind_hint_shapes_the_prompt_per_meeting_type():
    """種類的價值在於改變擷取重點，所以提示必須真的進到 prompt。"""
    from app.agents.decision_agent import KIND_HINTS, build_prompt

    prompt = build_prompt("逐字稿", date(2026, 8, 10), kind="銷售拜訪")
    assert "會議種類：銷售拜訪" in prompt
    assert KIND_HINTS["銷售拜訪"] in prompt
    # 每種類的提示都要不一樣，否則等於沒分類
    assert len(set(KIND_HINTS.values())) == len(KIND_HINTS)


def test_legacy_kind_values_still_resolve():
    """改版前存下來的會議帶的是舊的錄音種類值，不能因此讀不了或分析不了。"""
    from app.agents.decision_agent import MEETING_KINDS, resolve_kind

    assert resolve_kind("會議") == "一般會議"
    assert resolve_kind("講座") == "其它"  # 教育訓練也移除了，再往下對應
    assert resolve_kind("語音備忘錄") == "語音備忘錄"  # 這個種類留下來了
    assert resolve_kind(None) is None
    assert resolve_kind("銷售拜訪") == "銷售拜訪"
    assert "會議" not in MEETING_KINDS  # 舊值不再出現在選單


def test_per_meeting_terms_join_the_global_glossary_in_the_prompt():
    """本次專用詞彙要和全域詞彙表一起餵給模型，而不是取代它。"""
    from app.agents.decision_agent import build_prompt

    prompt = build_prompt(
        "逐字稿",
        date(2026, 8, 10),
        glossary=[{"term": "王霖翔", "note": "人名"}],
        extra_terms=[{"term": "TaskHub", "note": "本次專案代號"}],
    )
    assert "王霖翔（人名）" in prompt
    assert "TaskHub（本次專案代號）" in prompt


def test_per_meeting_terms_work_without_a_global_glossary():
    from app.agents.decision_agent import build_prompt

    prompt = build_prompt("逐字稿", date(2026, 8, 10), extra_terms=[{"term": "Kessel", "note": ""}])
    assert "Kessel" in prompt


# ---- 種類專屬輸出區塊（目前只有銷售拜訪的 BANT）----

def test_kind_sections_appear_in_schema_and_instructions():
    from app.agents.decision_agent import KIND_SECTIONS, build_prompt

    prompt = build_prompt("逐字稿", date(2026, 8, 10), kind="銷售拜訪")
    assert "sections" in prompt  # schema 範例
    # 光有 schema 不夠：要有明確的指示告訴模型只能用這幾個 label、每個都要輸出
    assert "sections 只能有下列" in prompt
    for label, desc in KIND_SECTIONS["銷售拜訪"].items():
        assert label in prompt
        assert desc in prompt  # 每個區塊要裝什麼也要講清楚


def test_section_rule_is_numbered_right_after_the_last_rule():
    """規則編號不能跳號：模型看到 10 之後直接跳 12 會以為自己漏了一條。"""
    from app.agents.decision_agent import build_prompt

    # 四項功能全開（沒有「這次不需要…」那條），區塊規則應該接在 11
    full = build_prompt("x", date(2026, 8, 10), kind="銷售拜訪")
    assert "\n11. sections" in full and "\n12." not in full
    # 有關掉的功能時，11 是「不需要…」，區塊規則接在 12
    partial = build_prompt("x", date(2026, 8, 10), kind="銷售拜訪", features={"summary"})
    assert "\n11. 這次不需要" in partial and "\n12. sections" in partial


def test_kinds_without_sections_do_not_get_the_field():
    """沒有專屬區塊的種類不該被要求輸出 sections，免得模型硬掰。"""
    from app.agents.decision_agent import build_prompt

    assert "sections" not in build_prompt("逐字稿", date(2026, 8, 10), kind="一般會議")


def test_sections_are_cleared_for_kinds_that_do_not_define_them():
    """模型有時會自作主張多輸出欄位；不屬於這個種類的區塊要被清掉。"""
    import json as _json
    from app.agents.decision_agent import DecisionAgent

    payload = _json.loads(valid_json())
    payload["sections"] = [{"label": "亂掰的區塊", "items": ["x"]}]
    agent = DecisionAgent(generate=lambda p: _json.dumps(payload, ensure_ascii=False))
    assert agent.analyze("測試", meeting_date=MEETING_DATE, kind="一般會議").sections == []


def test_sections_are_reordered_to_the_defined_order():
    """模型可能亂序或漏給；只留定義過的 label，並照定義的順序排好。"""
    import json as _json
    from app.agents.decision_agent import DecisionAgent

    payload = _json.loads(valid_json())
    payload["sections"] = [
        {"label": "需求", "items": ["要自動產出會議紀錄"]},
        {"label": "亂掰的", "items": ["x"]},
        {"label": "預算", "items": ["50 萬"]},
    ]
    agent = DecisionAgent(generate=lambda p: _json.dumps(payload, ensure_ascii=False))
    result = agent.analyze("測試", meeting_date=MEETING_DATE, kind="銷售拜訪")
    assert [s.label for s in result.sections] == ["預算", "需求"]
    assert result.sections[0].items == ["50 萬"]


# ---- 會議種類選單：幾份名單必須彼此對得上 ----

def test_menu_kinds_and_hints_stay_in_sync():
    """選單列的每個種類都要有分析提示，反之亦然。

    KIND_GROUPS（選單）與 KIND_HINTS（提示）是兩份分開維護的名單，
    增刪種類時很容易只改一邊——少了這個測試會慢慢對不上，而且不會報錯。
    """
    from app.agents.decision_agent import KIND_GROUPS, KIND_HINTS

    listed = [k for _, kinds in KIND_GROUPS for k in kinds]
    assert sorted(listed) == sorted(KIND_HINTS)
    assert len(listed) == len(set(listed)), "同一個種類不該出現在兩個分組"


def test_legacy_kinds_all_point_at_kinds_that_still_exist():
    """舊值的對應目標必須是還存在的種類。

    實際踩到的雷：把「教育訓練」從選單拿掉時，LEGACY_KINDS 裡的
    「講座 → 教育訓練」就懸空了——舊紀錄被對應到一個已不存在的種類，
    而這不會拋任何錯，只是分析提示默默消失，極難察覺。
    """
    from app.agents.decision_agent import LEGACY_KINDS, MEETING_KINDS

    for old, new in LEGACY_KINDS.items():
        assert new in MEETING_KINDS, f"「{old}」對應到已不存在的「{new}」"


def test_kinds_dropped_from_the_menu_still_open_old_records():
    """從選單移除的種類，既有紀錄仍要能讀取、重新分析與編輯——
    validate_kind 同時接受 MEETING_KINDS 與 LEGACY_KINDS，靠的就是這層對應。"""
    from app.agents.decision_agent import MEETING_KINDS, resolve_kind

    for gone in (
        "團隊站會", "專案啟動會", "專案進度會議", "設計技術評審",
        "回顧會議", "事故檢討", "教育訓練", "腦力激盪", "全體會議",
    ):
        assert resolve_kind(gone) in MEETING_KINDS, f"「{gone}」的舊紀錄會開不起來"


def test_project_group_is_a_single_kind():
    from app.agents.decision_agent import KIND_GROUPS

    assert dict(KIND_GROUPS)["專案"] == ["專案會議"]


def test_side_tables_only_mention_kinds_that_exist():
    """種類專屬區塊與預設功能表都不該留下已移除種類的殘骸。"""
    from app.agents.decision_agent import (
        KIND_DEFAULT_FEATURES,
        KIND_SECTIONS,
        MEETING_KINDS,
    )

    for kind in {**KIND_SECTIONS, **KIND_DEFAULT_FEATURES}:
        assert kind in MEETING_KINDS, f"「{kind}」已不在選單裡"
