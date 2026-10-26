"""任務的子項目與重複規則。

子任務讓「整理預算表」這種一句話其實是五件事的任務有地方拆；重複任務讓
週會的固定事項不用每週手動再打一次。
"""
from __future__ import annotations

import pytest

from app.taskextras import (
    MAX_SUBTASKS,
    RECURRENCES,
    TaskExtraError,
    clean_subtasks,
    next_occurrence,
    subtask_progress,
)


# ---- 子任務 ----

def test_strings_become_unchecked_items():
    assert clean_subtasks(["列出項目", "對帳"]) == [
        {"text": "列出項目", "done": False}, {"text": "對帳", "done": False}]


def test_existing_items_keep_their_tick():
    assert clean_subtasks([{"text": "列出項目", "done": True}]) == [
        {"text": "列出項目", "done": True}]


def test_blank_items_are_dropped():
    """按了「加一項」卻沒打字就送出，不該留下一個點不掉的空白列。"""
    assert clean_subtasks(["列出項目", "   ", ""]) == [{"text": "列出項目", "done": False}]


def test_whitespace_is_trimmed():
    assert clean_subtasks(["  對帳  "])[0]["text"] == "對帳"


def test_nothing_is_an_empty_list_not_an_error():
    assert clean_subtasks(None) == []
    assert clean_subtasks([]) == []


def test_too_many_items_are_rejected():
    with pytest.raises(TaskExtraError):
        clean_subtasks([f"第 {i} 項" for i in range(MAX_SUBTASKS + 1)])


def test_a_non_list_is_rejected():
    with pytest.raises(TaskExtraError):
        clean_subtasks("列出項目")


def test_progress_is_done_over_total():
    assert subtask_progress([{"text": "a", "done": True}, {"text": "b", "done": False}]) == (1, 2)


def test_progress_of_nothing():
    assert subtask_progress([]) == (0, 0)


# ---- 重複 ----

def test_the_known_rules():
    assert set(RECURRENCES) == {"daily", "weekly", "biweekly", "monthly"}


def test_daily_moves_one_day():
    assert next_occurrence("2026-10-26", "daily") == "2026-10-27"


def test_weekly_moves_seven_days():
    assert next_occurrence("2026-10-26", "weekly") == "2026-11-02"


def test_biweekly_moves_fourteen_days():
    assert next_occurrence("2026-10-26", "biweekly") == "2026-11-09"


def test_monthly_keeps_the_day_of_month():
    assert next_occurrence("2026-10-26", "monthly") == "2026-11-26"


def test_monthly_from_the_end_of_a_long_month_lands_on_the_last_day():
    """1/31 的下個月沒有 31 號。多跑到 3/3 的話，每個月都會再往後漂一點。"""
    assert next_occurrence("2026-01-31", "monthly") == "2026-02-28"


def test_monthly_across_a_year_boundary():
    assert next_occurrence("2026-12-15", "monthly") == "2027-01-15"


def test_an_unknown_rule_produces_nothing():
    assert next_occurrence("2026-10-26", "每隔三天") is None


def test_a_task_with_no_due_date_cannot_repeat():
    """沒有期限的重複任務等於無限產生一堆沒有期限的任務。"""
    assert next_occurrence(None, "weekly") is None
    assert next_occurrence("", "weekly") is None


def test_a_broken_date_produces_nothing_instead_of_blowing_up():
    assert next_occurrence("下禮拜三", "weekly") is None
