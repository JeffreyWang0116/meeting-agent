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

    prompt = build_prompt("測試", MEETING_DATE, kind="講座")
    assert "錄音種類：講座" in prompt
    # 沒指定種類時不出現種類段落（維持通用行為）
    assert "錄音種類" not in build_prompt("測試", MEETING_DATE)


def test_analyze_passes_kind_into_prompt():
    captured = {}

    def fake_generate(prompt):
        captured["prompt"] = prompt
        return valid_json()

    DecisionAgent(generate=fake_generate).analyze(
        "測試", meeting_date=MEETING_DATE, kind="訪談"
    )
    assert "錄音種類：訪談" in captured["prompt"]


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
