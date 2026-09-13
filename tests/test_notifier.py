"""Notifier Agent：確認信草稿與行事曆事件產生測試。"""
import json
import re

import pytest

from app.agents.notifier_agent import NotifierAgent, build_calendar_events
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


def test_email_subject_returned_separately(tmp_path, analysis):
    """前端要把主旨與內文分開塞進 Gmail/mailto 的 su= 與 body=，不能自己剖字串。"""
    result = NotifierAgent(tmp_path).notify("m001", analysis)
    assert result["email_subject"] == "【會議紀錄確認】專題進度會議（2026-07-12）"


def test_email_draft_first_line_matches_subject(tmp_path, analysis):
    """草稿全文（複製用）仍帶主旨行，且與 email_subject 是同一份內容。"""
    result = NotifierAgent(tmp_path).notify("m001", analysis)
    assert result["email_draft"].split("\n")[0] == f"主旨：{result['email_subject']}"


def test_email_draft_handles_missing_summary(tmp_path):
    """summary 功能沒被使用（None）時，草稿不能出現 Python 的 "None" 字樣。"""
    payload = make_valid_payload()
    payload["meeting"]["summary"] = None
    analysis = MeetingAnalysis.model_validate(payload)
    draft = NotifierAgent(tmp_path).notify("m001", analysis)["email_draft"]
    assert "None" not in draft


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


# ---- 事件 id：讓「加入 Google 行事曆」按兩次不會變成兩份 ----
# Google 的 events.insert 允許自帶 id，重複 insert 同一個 id 會回 409，
# 前端就靠這個分辨「已經加過」而不是再塞一筆。

# Google 規定 event id 只能用 base32hex 的字元（a-v、0-9），長度 5~1024
GOOGLE_EVENT_ID_RE = re.compile(r"^[a-v0-9]{5,1024}$")


def test_calendar_events_carry_google_compatible_ids(analysis):
    events = build_calendar_events(analysis, meeting_id="m001")
    assert events
    for event in events:
        assert GOOGLE_EVENT_ID_RE.match(event["id"]), event["id"]


def test_event_id_is_stable_across_calls(analysis):
    """同一筆代辦每次都要算出同一個 id，否則按第二次又會插入一份新的。"""
    first = build_calendar_events(analysis, meeting_id="m001")
    second = build_calendar_events(analysis, meeting_id="m001")
    assert [e["id"] for e in first] == [e["id"] for e in second]


def test_event_id_differs_by_task_and_meeting(analysis):
    base = build_calendar_events(analysis, meeting_id="m001")[0]["id"]

    other_meeting = build_calendar_events(analysis, meeting_id="m002")[0]["id"]
    assert other_meeting != base, "不同會議的同名代辦是兩件事，不該互相蓋掉"

    analysis.todos[0].task = "完成 Prompt 第二版"
    other_task = build_calendar_events(analysis, meeting_id="m001")[0]["id"]
    assert other_task != base


def test_events_written_by_notify_also_carry_ids(tmp_path, analysis):
    result = NotifierAgent(tmp_path).notify("m001", analysis)
    assert all(e.get("id") for e in result["calendar_events"])
