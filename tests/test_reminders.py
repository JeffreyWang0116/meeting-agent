"""Reminder Agent 測試：主動掃描任務庫、產生催辦與追問草稿。

「主動式」的核心：agent 不等使用者開口，自己找出逾期、即將到期、
沒人負責的任務與議而未決的事項，並擬好可直接送出的訊息。
"""
from datetime import date

from app.agents.reminder_agent import scan

TODAY = date(2026, 7, 13)


def make_task(**over):
    task = {
        "id": "t1",
        "meeting_id": "m1",
        "task": "完成 Prompt 初版",
        "owner": "王鈺翔",
        "due_date": "2026-07-20",
        "priority": "high",
        "status": "todo",
    }
    task.update(over)
    return task


def make_meeting(**over):
    meeting = {
        "id": "m1",
        "created_at": "2026-07-12T09:00:00+00:00",
        "meeting": {"title": "專題進度會議", "date": "2026-07-12"},
        "pending_items": [],
    }
    meeting.update(over)
    return meeting


# ---- 逾期 ----

def test_overdue_task_generates_reminder_with_days():
    result = scan([make_task(due_date="2026-07-10")], [], today=TODAY)
    [r] = result["reminders"]
    assert r["kind"] == "overdue"
    assert r["days"] == 3
    assert "完成 Prompt 初版" in r["message"]
    assert "逾期" in r["message"]
    assert "王鈺翔" in r["message"]


def test_done_task_is_never_reminded():
    result = scan([make_task(due_date="2026-07-10", status="done")], [], today=TODAY)
    assert result["reminders"] == []


# ---- 即將到期 ----

def test_due_soon_within_window():
    result = scan([make_task(due_date="2026-07-15")], [], today=TODAY, due_soon_days=2)
    [r] = result["reminders"]
    assert r["kind"] == "due_soon"
    assert r["days"] == 2


def test_due_today_counts_as_due_soon_zero_days():
    result = scan([make_task(due_date="2026-07-13")], [], today=TODAY)
    [r] = result["reminders"]
    assert r["kind"] == "due_soon"
    assert r["days"] == 0


def test_due_beyond_window_not_reminded():
    result = scan([make_task(due_date="2026-07-20")], [], today=TODAY, due_soon_days=2)
    assert result["reminders"] == []


# ---- 沒人負責 ----

def test_unassigned_task_without_due_date_reminded():
    result = scan([make_task(owner=None, due_date=None)], [], today=TODAY)
    [r] = result["reminders"]
    assert r["kind"] == "unassigned"
    assert "負責人" in r["message"]


def test_overdue_and_unassigned_yields_single_overdue_reminder():
    """一個任務只提醒一次：逾期優先於未指派，但草稿要提到沒人負責。"""
    result = scan([make_task(owner=None, due_date="2026-07-10")], [], today=TODAY)
    [r] = result["reminders"]
    assert r["kind"] == "overdue"
    assert "負責人" in r["message"]


def test_invalid_due_date_treated_as_none():
    result = scan([make_task(due_date="下週五")], [], today=TODAY)
    assert result["reminders"] == []  # 有負責人、日期無法解析 → 不提醒


# ---- 排序 ----

def test_reminders_sorted_overdue_first_then_due_soon():
    tasks = [
        make_task(id="a", due_date="2026-07-14"),               # due_soon 1 天
        make_task(id="b", due_date="2026-07-01"),               # 逾期 12 天
        make_task(id="c", owner=None, due_date=None),           # 未指派
        make_task(id="d", due_date="2026-07-11"),               # 逾期 2 天
    ]
    kinds = [r["kind"] for r in scan(tasks, [], today=TODAY)["reminders"]]
    ids = [r["task"]["id"] for r in scan(tasks, [], today=TODAY)["reminders"]]
    assert kinds == ["overdue", "overdue", "due_soon", "unassigned"]
    assert ids[:2] == ["b", "d"]  # 逾期越久越前面


# ---- 未決事項追問 ----

def test_pending_items_generate_followup_drafts():
    meeting = make_meeting(
        pending_items=[{"topic": "要不要支援英文介面", "reason": "等指導教授意見"}]
    )
    result = scan([], [meeting], today=TODAY)
    [f] = result["followups"]
    assert f["meeting_title"] == "專題進度會議"
    assert f["topic"] == "要不要支援英文介面"
    assert "要不要支援英文介面" in f["message"]
    assert "等指導教授意見" in f["message"]


def test_generated_at_included():
    assert scan([], [], today=TODAY)["generated_at"] == "2026-07-13"
