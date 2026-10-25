"""用一句話請 AI 修改分析結果（摘要改短、負責人改成小明…）。

重跑整份分析太貴也太粗暴——改一個負責人不該讓所有任務換新 id。所以改成讓
模型只回「要改哪些欄位」的修改單，本地套用，而且先給使用者看過才寫進去。

紅線：逐字稿不給改。逐字稿是事實紀錄，要改走就地修正那條路；讓模型重寫它
等於讓它偽造證據，引用原句也會跟著對不回去。
"""
from __future__ import annotations

import pytest

from app.refine import RefineError, apply_patch, parse_patch, refine_prompt

RECORD = {
    "meeting": {"title": "專題進度會議", "date": "2026-10-20",
                "summary": "討論了預算與期程，決定維持原案。", "attendees": ["講者A", "講者B"]},
    "decisions": [{"description": "預算維持原案", "context": None}],
    "pending_items": [{"topic": "場地", "reason": "還沒問到"}],
    "highlights": [{"text": "談到預算", "time": "0:01", "source_quote": "今天要談預算"}],
    "sections": [{"label": "風險", "items": ["時程可能延後"]}],
    "tags": ["專題"],
    "transcript": "[0:01] 講者A：今天要談預算",
}
TASKS = [
    {"id": "t1", "task": "整理預算表", "owner": "講者A", "due_date": "2026-10-30", "priority": "high"},
    {"id": "t2", "task": "訂場地", "owner": None, "due_date": None, "priority": "medium"},
]


# ---- 提示詞 ----

def test_prompt_carries_the_request_and_the_current_content():
    out = refine_prompt("把摘要改短一點", RECORD, TASKS)

    assert "把摘要改短一點" in out
    assert "討論了預算與期程" in out
    assert "整理預算表" in out


def test_prompt_forbids_touching_the_transcript():
    out = refine_prompt("改寫逐字稿", RECORD, TASKS)

    assert "逐字稿" in out
    assert "transcript" in out


# ---- 解析模型回來的修改單 ----

def test_parses_meeting_field_changes():
    patch = parse_patch('{"meeting": {"summary": "預算維持原案。"}}')

    assert patch["meeting"]["summary"] == "預算維持原案。"


def test_parses_task_changes_by_id():
    patch = parse_patch('{"tasks": [{"id": "t1", "owner": "王小明"}]}')

    assert patch["tasks"] == [{"id": "t1", "owner": "王小明"}]


def test_code_fence_is_tolerated():
    patch = parse_patch('```json\n{"meeting": {"title": "新標題"}}\n```')

    assert patch["meeting"]["title"] == "新標題"


def test_unparseable_reply_is_an_error_not_a_silent_no_op():
    with pytest.raises(RefineError):
        parse_patch("我不太確定你的意思")


def test_transcript_in_the_patch_is_rejected_outright():
    """模型被要求不要碰，但不能只靠它守規矩。"""
    with pytest.raises(RefineError):
        parse_patch('{"transcript": "偽造的逐字稿"}')


def test_unknown_top_level_keys_are_rejected():
    with pytest.raises(RefineError):
        parse_patch('{"user": "someone-else"}')


def test_task_change_without_an_id_is_rejected():
    with pytest.raises(RefineError):
        parse_patch('{"tasks": [{"owner": "王小明"}]}')


def test_task_id_that_is_not_in_this_meeting_is_rejected():
    with pytest.raises(RefineError):
        apply_patch(RECORD, TASKS, {"tasks": [{"id": "someone-elses", "owner": "x"}]})


# ---- 套用 ----

def test_applying_only_touches_the_named_fields():
    record, tasks, changes = apply_patch(RECORD, TASKS, {"meeting": {"summary": "短摘要"}})

    assert record["meeting"]["summary"] == "短摘要"
    assert record["meeting"]["title"] == "專題進度會議"  # 沒點名的不動
    assert tasks == TASKS
    assert changes == [{"what": "摘要", "before": RECORD["meeting"]["summary"], "after": "短摘要"}]


def test_applying_a_task_change_keeps_the_same_task_id():
    """重新分析會把任務整批換新 id，改一個負責人不該有那種副作用。"""
    _, tasks, _ = apply_patch(RECORD, TASKS, {"tasks": [{"id": "t1", "owner": "王小明"}]})

    assert tasks[0]["id"] == "t1"
    assert tasks[0]["owner"] == "王小明"
    assert tasks[0]["task"] == "整理預算表"


def test_changes_are_listed_for_the_preview():
    _, _, changes = apply_patch(
        RECORD, TASKS,
        {"meeting": {"title": "新標題"}, "tasks": [{"id": "t2", "owner": "小華"}]},
    )

    assert len(changes) == 2
    assert any("小華" == c["after"] for c in changes)


def test_a_patch_that_changes_nothing_reports_no_changes():
    _, _, changes = apply_patch(RECORD, TASKS, {"meeting": {"title": "專題進度會議"}})

    assert changes == []


def test_the_original_record_is_not_mutated():
    apply_patch(RECORD, TASKS, {"meeting": {"summary": "短摘要"}})

    assert RECORD["meeting"]["summary"] == "討論了預算與期程，決定維持原案。"


def test_bad_due_date_is_rejected_rather_than_stored():
    with pytest.raises(RefineError):
        apply_patch(RECORD, TASKS, {"tasks": [{"id": "t1", "due_date": "下週三"}]})


def test_bad_priority_is_rejected():
    with pytest.raises(RefineError):
        apply_patch(RECORD, TASKS, {"tasks": [{"id": "t1", "priority": "超急"}]})
