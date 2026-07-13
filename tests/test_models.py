"""MeetingAnalysis JSON schema 驗證測試。

Decision Agent 的產出契約：LLM 回傳的 JSON 必須能通過這裡的驗證。
"""
import json

import pytest
from pydantic import ValidationError

from app.models import MeetingAnalysis, TodoItem


def make_valid_payload():
    return {
        "meeting": {
            "title": "專題進度會議",
            "date": "2026-07-12",
            "summary": "討論 7 月里程碑進度與分工。",
            "attendees": ["王鈺翔", "Kevin"],
        },
        "decisions": [
            {"description": "採用 FastAPI 作為後端框架", "context": "行動端之後可共用 API"}
        ],
        "todos": [
            {
                "task": "完成 Prompt 初版",
                "owner": "王鈺翔",
                "due_date": "2026-07-20",
                "priority": "high",
                "source_quote": "鈺翔你下週一前把 prompt 寫好",
            }
        ],
        "pending_items": [
            {"topic": "要不要支援英文介面", "reason": "等指導教授意見"}
        ],
    }


def test_valid_payload_parses():
    analysis = MeetingAnalysis.model_validate(make_valid_payload())
    assert analysis.meeting.title == "專題進度會議"
    assert analysis.todos[0].priority == "high"
    assert str(analysis.todos[0].due_date) == "2026-07-20"


def test_parses_from_json_string():
    raw = json.dumps(make_valid_payload(), ensure_ascii=False)
    analysis = MeetingAnalysis.model_validate_json(raw)
    assert analysis.meeting.attendees == ["王鈺翔", "Kevin"]


def test_owner_and_due_date_may_be_null():
    payload = make_valid_payload()
    payload["todos"][0]["owner"] = None
    payload["todos"][0]["due_date"] = None
    analysis = MeetingAnalysis.model_validate(payload)
    assert analysis.todos[0].owner is None
    assert analysis.todos[0].due_date is None


def test_invalid_priority_rejected():
    payload = make_valid_payload()
    payload["todos"][0]["priority"] = "urgent"
    with pytest.raises(ValidationError):
        MeetingAnalysis.model_validate(payload)


def test_invalid_date_rejected():
    payload = make_valid_payload()
    payload["todos"][0]["due_date"] = "下週五"
    with pytest.raises(ValidationError):
        MeetingAnalysis.model_validate(payload)


def test_lists_default_to_empty():
    payload = {
        "meeting": {
            "title": "簡短會議",
            "date": "2026-07-12",
            "summary": "無具體結論。",
            "attendees": [],
        }
    }
    analysis = MeetingAnalysis.model_validate(payload)
    assert analysis.decisions == []
    assert analysis.todos == []
    assert analysis.pending_items == []


def test_todo_priority_defaults_to_medium():
    item = TodoItem(task="測試任務")
    assert item.priority == "medium"


def test_round_trip_serialization():
    analysis = MeetingAnalysis.model_validate(make_valid_payload())
    dumped = analysis.model_dump(mode="json")
    assert dumped["todos"][0]["due_date"] == "2026-07-20"
    assert MeetingAnalysis.model_validate(dumped) == analysis
