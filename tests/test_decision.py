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


def test_meeting_date_defaults_to_today():
    captured = {}

    def fake_generate(prompt):
        captured["prompt"] = prompt
        return valid_json()

    DecisionAgent(generate=fake_generate).analyze("測試")
    assert str(date.today()) in captured["prompt"]
