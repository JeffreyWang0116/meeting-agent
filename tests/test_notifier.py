"""Notifier Agent：確認信草稿與行事曆事件產生測試。"""
import json

import pytest

from app.agents.notifier_agent import NotifierAgent
from app.models import MeetingAnalysis
from tests.test_models import make_valid_payload


@pytest.fixture
def analysis():
    return MeetingAnalysis.model_validate(make_valid_payload())


def test_notify_writes_email_and_calendar_files(tmp_path, analysis):
    notifier = NotifierAgent(tmp_path)
    result = notifier.notify("m001", analysis)

    email_path = tmp_path / "m001" / "email_draft.txt"
    events_path = tmp_path / "m001" / "calendar_events.json"
    assert email_path.exists()
    assert events_path.exists()
    assert result["email_draft_path"] == str(email_path)
    assert result["calendar_events_path"] == str(events_path)


def test_email_draft_contains_key_sections(tmp_path, analysis):
    result = NotifierAgent(tmp_path).notify("m001", analysis)
    draft = result["email_draft"]

    assert "專題進度會議" in draft
    assert "2026-07-12" in draft
    assert "討論 7 月里程碑進度與分工。" in draft          # 摘要
    assert "採用 FastAPI 作為後端框架" in draft            # 決議
    assert "完成 Prompt 初版" in draft                     # 代辦
    assert "王鈺翔" in draft                               # 負責人
    assert "要不要支援英文介面" in draft                   # 待確認
    assert "高" in draft                                   # 優先級中文化


def test_calendar_event_shape_matches_google_api(tmp_path, analysis):
    result = NotifierAgent(tmp_path).notify("m001", analysis)
    events = result["calendar_events"]

    assert len(events) == 1
    event = events[0]
    assert event["summary"] == "【代辦】完成 Prompt 初版"
    assert event["start"] == {"date": "2026-07-20"}
    assert event["end"] == {"date": "2026-07-21"}  # Google 全天事件 end 為隔天（exclusive）
    assert "王鈺翔" in event["description"]


def test_todos_without_due_date_get_no_calendar_event(tmp_path, analysis):
    analysis.todos[0].due_date = None
    result = NotifierAgent(tmp_path).notify("m001", analysis)
    assert result["calendar_events"] == []


def test_unassigned_owner_shown_as_pending(tmp_path, analysis):
    analysis.todos[0].owner = None
    result = NotifierAgent(tmp_path).notify("m001", analysis)
    assert "未指派" in result["email_draft"]


def test_events_file_is_valid_json(tmp_path, analysis):
    NotifierAgent(tmp_path).notify("m001", analysis)
    data = json.loads((tmp_path / "m001" / "calendar_events.json").read_text(encoding="utf-8"))
    assert isinstance(data, list)
