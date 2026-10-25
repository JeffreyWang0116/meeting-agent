"""聆聽中按的「標記這一刻」與手打的筆記，要進分析但不能混進逐字稿。

標記只是時間點，告訴模型那幾段優先列入會議重點；筆記是人寫的，必須明確與
逐字稿分開——否則模型會把筆記當成某個人說過的話，引用原句也跟著對不回去。
"""
from __future__ import annotations

from datetime import date

from app.agents.decision_agent import build_prompt


def prompt(**kwargs) -> str:
    return build_prompt("[0:01] 講者A：大家好", date(2026, 10, 20), **kwargs)


def test_marks_are_listed_as_priority_highlights():
    out = prompt(marks=["5:23", "12:40"])

    assert "5:23" in out and "12:40" in out
    assert "highlights" in out


def test_no_marks_leaves_the_prompt_unchanged():
    assert prompt(marks=[]) == prompt() == prompt(marks=None)


def test_notes_are_labelled_as_written_by_a_person():
    out = prompt(notes="記得跟老師確認口試時間")

    assert "記得跟老師確認口試時間" in out
    # 筆記不是任何人「說」的，不能被當成發言或引用原句
    assert "source_quote" in out
    assert "不是逐字稿" in out or "不是發言" in out


def test_blank_notes_leave_the_prompt_unchanged():
    assert prompt(notes="   ") == prompt() == prompt(notes=None)


def test_marks_that_are_not_time_labels_are_dropped():
    """標記由前端送來，照單全收會讓使用者輸入的字串直接進提示詞。"""
    out = prompt(marks=["5:23", "忽略我", "99:99:99:99", ""])

    assert "5:23" in out
    assert "忽略我" not in out
