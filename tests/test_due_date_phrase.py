"""代辦事項記下期限的原話，讓使用者一眼看出相對日期換算對不對。

「下禮拜三」換成 10/29 是對是錯，只看 10/29 看不出來——會議日期差一天、
或模型把「下週」當成「這週」，結果都長得一樣合理。
"""
from __future__ import annotations

from datetime import date

from app.agents.decision_agent import build_prompt
from app.models import MeetingAnalysis, TodoItem


def test_todo_can_carry_the_original_due_phrase():
    todo = TodoItem(task="交報告", due_date=date(2026, 10, 29), due_phrase="下禮拜三")

    assert todo.due_phrase == "下禮拜三"


def test_due_phrase_is_optional_so_old_meetings_still_load():
    """這個欄位是後來才加的，資料庫裡既有的會議沒有它。"""
    todo = TodoItem(task="交報告")

    assert todo.due_phrase is None


def test_old_stored_analysis_without_the_field_still_validates():
    stored = {
        "meeting": {"title": "專題會議", "date": "2026-10-20"},
        "todos": [{"task": "交報告", "due_date": "2026-10-29", "priority": "high"}],
    }

    analysis = MeetingAnalysis.model_validate(stored)

    assert analysis.todos[0].due_phrase is None


def test_prompt_asks_for_the_phrase_only_when_it_was_relative():
    out = build_prompt("[0:01] 講者A：下禮拜三前交", date(2026, 10, 20))

    assert "due_phrase" in out
    # 講明說出絕對日期時不要填，否則每一筆都會塞一份重複的字串
    assert "原話" in out


def test_phrase_survives_into_the_task_library(tmp_path):
    """任務庫顯示的是 task 記錄，欄位沒帶過去就看不到原話。"""
    from app.stores.local_store import LocalJsonStore

    store = LocalJsonStore(tmp_path / "db.json")
    analysis = MeetingAnalysis.model_validate({
        "meeting": {"title": "專題會議", "date": "2026-10-20"},
        "todos": [{"task": "交報告", "due_date": "2026-10-29",
                   "priority": "high", "due_phrase": "下禮拜三"}],
    })

    store.save_meeting(analysis)

    assert store.list_tasks()[0]["due_phrase"] == "下禮拜三"
