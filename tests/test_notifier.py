"""Notifier Agent：確認信草稿與行事曆事件產生測試。"""
import re

import pytest

from app.agents.notifier_agent import build_calendar_events, build_notifications
from app.models import MeetingAnalysis
from tests.test_models import make_valid_payload


@pytest.fixture
def analysis():
    return MeetingAnalysis.model_validate(make_valid_payload())


def test_notifications_carry_no_server_filesystem_paths(analysis):
    """回傳值會原封不動變成 /api/meetings 的回應，公開部署不能夾帶容器內的路徑。"""
    result = build_notifications(analysis, "m001")

    assert result["email_draft"] and result["calendar_events"]
    leaked = [k for k in result if k.endswith("_path")]
    assert not leaked, f"回應仍夾帶伺服器路徑：{leaked}"


def test_email_draft_contains_key_sections(analysis):
    result = build_notifications(analysis, "m001")
    draft = result["email_draft"]

    assert "專題進度會議" in draft
    assert "2026-07-12" in draft
    assert "討論 7 月里程碑進度與分工。" in draft          # 摘要
    assert "採用 FastAPI 作為後端框架" in draft            # 決議
    assert "完成 Prompt 初版" in draft                     # 代辦
    assert "陳志明" in draft                               # 負責人
    assert "要不要支援英文介面" in draft                   # 待確認
    assert "高" in draft                                   # 優先級中文化


def test_email_subject_returned_separately(analysis):
    """前端要把主旨與內文分開塞進 Gmail/mailto 的 su= 與 body=，不能自己剖字串。"""
    result = build_notifications(analysis, "m001")
    assert result["email_subject"] == "【會議紀錄確認】專題進度會議（2026-07-12）"


def test_email_draft_first_line_matches_subject(analysis):
    """草稿全文（複製用）仍帶主旨行，且與 email_subject 是同一份內容。"""
    result = build_notifications(analysis, "m001")
    assert result["email_draft"].split("\n")[0] == f"主旨：{result['email_subject']}"


def test_email_draft_handles_missing_summary():
    """summary 功能沒被使用（None）時，草稿不能出現 Python 的 "None" 字樣。"""
    payload = make_valid_payload()
    payload["meeting"]["summary"] = None
    analysis = MeetingAnalysis.model_validate(payload)
    draft = build_notifications(analysis, "m001")["email_draft"]
    assert "None" not in draft


def test_calendar_event_shape_matches_google_api(analysis):
    result = build_notifications(analysis, "m001")
    events = result["calendar_events"]

    assert len(events) == 1
    event = events[0]
    assert event["summary"] == "【代辦】完成 Prompt 初版"
    assert event["start"] == {"date": "2026-07-20"}
    assert event["end"] == {"date": "2026-07-21"}  # Google 全天事件 end 為隔天（exclusive）
    assert "陳志明" in event["description"]


def test_todos_without_due_date_get_no_calendar_event(analysis):
    analysis.todos[0].due_date = None
    result = build_notifications(analysis, "m001")
    assert result["calendar_events"] == []


def test_unassigned_owner_shown_as_pending(analysis):
    analysis.todos[0].owner = None
    result = build_notifications(analysis, "m001")
    assert "未指派" in result["email_draft"]


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


def test_notifications_events_carry_ids(analysis):
    result = build_notifications(analysis, "m001")
    assert all(e.get("id") for e in result["calendar_events"])
