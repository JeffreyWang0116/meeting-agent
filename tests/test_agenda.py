"""會議前準備：貼議程讓 AI 逐項對照，並把上次沒做完的帶到這次。

散會後最常見的兩個問題是「議程上有一項好像沒講到」和「上次講好的那件事
後來呢」。兩個都不必重讀逐字稿才回答得出來——議程是會前就寫好的，上次
沒做完的事資料庫裡也有。
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from app.agenda import (
    MAX_AGENDA_ITEMS,
    carry_over_lines,
    clean_agenda,
    coverage_summary,
)
from app.agents.decision_agent import build_prompt

AGENDA = ["預算審查", "期程確認", "場地"]


# ---- 議程清單 ----

def test_one_item_per_line():
    assert clean_agenda("預算審查\n期程確認\n場地") == AGENDA


def test_numbering_and_bullets_are_stripped():
    """使用者會直接從會議通知複製貼上，那裡面本來就有編號。"""
    assert clean_agenda("1. 預算審查\n2) 期程確認\n- 場地\n• 其他") == [*AGENDA, "其他"]


def test_blank_lines_are_dropped():
    assert clean_agenda("預算審查\n\n  \n期程確認") == ["預算審查", "期程確認"]


def test_duplicates_are_dropped():
    """重複的項目會讓 AI 輸出兩筆一樣的對照結果。"""
    assert clean_agenda("預算審查\n預算審查") == ["預算審查"]


def test_too_many_items_are_cut_off():
    out = clean_agenda("\n".join(f"第 {i} 項" for i in range(MAX_AGENDA_ITEMS + 10)))

    assert len(out) == MAX_AGENDA_ITEMS


def test_nothing_in_nothing_out():
    assert clean_agenda("") == []
    assert clean_agenda(None) == []


# ---- 帶到下次 ----

def test_unfinished_tasks_and_open_questions_come_across():
    lines = carry_over_lines(
        tasks=[{"task": "整理預算表", "owner": "小明", "status": "todo"},
               {"task": "訂場地", "owner": None, "status": "done"}],
        pending=[{"topic": "場地", "reason": "還沒問到"}],
    )

    assert any("整理預算表" in line and "小明" in line for line in lines)
    assert any("場地" in line for line in lines)


def test_finished_tasks_do_not_come_across():
    """做完的事還一直被帶到下次，清單很快就沒人看了。"""
    lines = carry_over_lines(tasks=[{"task": "交報告", "status": "done"}], pending=[])

    assert lines == []


def test_an_unassigned_task_says_so():
    lines = carry_over_lines(tasks=[{"task": "訂場地", "owner": None, "status": "todo"}], pending=[])

    assert "未指派" in lines[0]


# ---- 提示詞 ----

def test_the_agenda_goes_into_the_prompt():
    out = build_prompt("逐字稿", date(2026, 10, 26), agenda=AGENDA)

    for item in AGENDA:
        assert item in out
    assert "agenda_review" in out


def test_the_prompt_forbids_inventing_coverage():
    """「每一項都討論過了」是最舒服也最沒用的答案。"""
    out = build_prompt("逐字稿", date(2026, 10, 26), agenda=AGENDA)

    assert "missed" in out and "partial" in out


def test_off_agenda_topics_are_asked_for():
    out = build_prompt("逐字稿", date(2026, 10, 26), agenda=AGENDA)

    assert "off_agenda" in out


def test_without_an_agenda_the_prompt_does_not_mention_it():
    """沒有議程時多一段「沒有議程」的說明只是白白佔 token。"""
    out = build_prompt("逐字稿", date(2026, 10, 26))

    assert "agenda_review" not in out


def test_carry_over_goes_into_the_prompt():
    out = build_prompt("逐字稿", date(2026, 10, 26), carry_over=["整理預算表（小明，未完成）"])

    assert "整理預算表" in out
    assert "上次" in out


def test_the_rules_are_numbered_without_a_gap():
    """模型看到 10 之後直接跳 12，會以為自己漏讀了一條。"""
    import re

    out = build_prompt("逐字稿", date(2026, 10, 26), agenda=AGENDA, carry_over=["上次的事"])
    numbers = [int(m.group(1)) for m in re.finditer(r"^(\d{1,2})\. ", out, re.M)]

    assert numbers == list(range(1, len(numbers) + 1))
    assert len(numbers) >= 12  # 內建 10 條 + 議程 + 上次未完成


# ---- 對照結果的整理 ----

def test_coverage_counts_each_status():
    review = [{"item": "a", "status": "covered"}, {"item": "b", "status": "missed"},
              {"item": "c", "status": "partial"}, {"item": "d", "status": "covered"}]

    assert coverage_summary(review) == {"covered": 2, "partial": 1, "missed": 1}


def test_an_unknown_status_is_counted_as_missed():
    """模型偶爾會回別的字。當成「討論過」會讓人以為事情辦完了。"""
    assert coverage_summary([{"item": "a", "status": "不確定"}]) == {
        "covered": 0, "partial": 0, "missed": 1}


def test_an_empty_review_counts_nothing():
    assert coverage_summary([]) == {"covered": 0, "partial": 0, "missed": 0}


@pytest.mark.parametrize("bad", [None, "字串", 123])
def test_rubbish_in_the_review_does_not_blow_up(bad):
    assert coverage_summary(bad) == {"covered": 0, "partial": 0, "missed": 0}


def test_a_review_without_an_agenda_is_thrown_away():
    """沒有議程卻回了議程對照，那份對照是憑空生出來的。"""
    from app.agents.decision_agent import DecisionAgent

    reply = json.dumps({
        "meeting": {"title": "x", "date": "2026-10-26"},
        "agenda_review": [{"item": "沒人提過的議程", "status": "covered"}],
        "off_agenda": ["也是編的"],
    }, ensure_ascii=False)

    out = DecisionAgent(generate=lambda p: reply).analyze("逐字稿", date(2026, 10, 26))

    assert out.agenda_review == []
    assert out.off_agenda == []
