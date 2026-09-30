"""講者改名：逐字稿講者欄、出席者、以及摘要／重點／決議等自由文字。"""
from app.speaker_rename import (
    name_problem,
    rename_in_list,
    rename_in_text,
    rename_meeting_fields,
    rename_speaker_in_transcript,
    rename_task_fields,
)


def test_rename_only_touches_the_speaker_field():
    text = "[0:21] 講者A：王委員您好\n講者A：續行\n[2:08] 講者B：謝謝，剛剛講者A問的\n[2:30]講者AB：不是同一人"
    assert rename_speaker_in_transcript(text, "講者A", "翁曉玲") == (
        "[0:21] 翁曉玲：王委員您好\n翁曉玲：續行\n[2:08] 講者B：謝謝，剛剛講者A問的\n[2:30]講者AB：不是同一人"
    )


def test_rename_accepts_halfwidth_colon_and_leading_spaces():
    out = rename_speaker_in_transcript("  [1:02:03] Speaker 2: hi", "Speaker 2", "Kevin Lin")
    assert out == "  [1:02:03] Kevin Lin: hi"


def test_rename_treats_regex_characters_literally():
    assert rename_speaker_in_transcript("A.B：x\nAxB：y", "A.B", "王") == "王：x\nAxB：y"


def test_rename_transcript_handles_none():
    assert rename_speaker_in_transcript(None, "講者A", "翁曉玲") == ""


def test_attendees_are_renamed_without_duplicates():
    assert rename_in_list(["講者A", "講者B", "翁曉玲"], "講者A", "翁曉玲") == ["翁曉玲", "講者B"]


def test_name_problem_blocks_names_that_break_the_transcript():
    """冒號會在行首造出假講者、換行會拆行、方括號會被當成時間標記——與前端 speakerNameProblem 同一套。"""
    for bad in ["", "  ", "王委員：他說", "a:b", "兩\n行", "[1:00]", "王" * 21]:
        assert name_problem(bad), bad
    assert name_problem(" 翁曉玲 ") == ""


# ---- 自由文字：沒有講者欄可以對，要防誤換 ----

def test_text_code_is_replaced_but_not_a_longer_code():
    text = "講者A質疑調查案件偏少，講者B回應；講者AB未發言。Speaker 2 agreed, Speaker 20 left."
    assert rename_in_text(text, "講者A", "翁曉玲") == (
        "翁曉玲質疑調查案件偏少，講者B回應；講者AB未發言。Speaker 2 agreed, Speaker 20 left."
    )
    assert "Kevin agreed, Speaker 20 left" in rename_in_text(text, "Speaker 2", "Kevin")


def test_text_rename_does_not_double_up_when_new_name_contains_old():
    """把「翁曉」改成「翁曉玲」時，已經寫對的「翁曉玲」不能變成「翁曉玲玲」。"""
    assert rename_in_text("翁曉提問，翁曉玲追問", "翁曉", "翁曉玲") == "翁曉玲提問，翁曉玲追問"


def test_text_rename_skips_single_character_names():
    """單字名在中文裡到處都是（「王」「林」），整段取代會誤傷。"""
    assert rename_in_text("王委員說明，王先生補充", "王", "王榮璋") == "王委員說明，王先生補充"


def test_text_rename_keeps_empty_values():
    assert rename_in_text(None, "講者A", "翁曉玲") is None
    assert rename_in_text("", "講者A", "翁曉玲") == ""


# ---- 整場會議與任務 ----

def meeting_record():
    return {
        "id": "m1",
        "meeting": {"title": "講者A 的一對一", "summary": "講者A質疑預算", "attendees": ["講者A", "講者B"]},
        "transcript": "[0:01] 講者A：預算太少\n[0:05] 講者B：剛剛講者A說的",
        "highlights": [{"text": "講者A質疑預算", "time": "0:01", "source_quote": "講者A：預算太少"}],
        "decisions": [{"description": "講者A 下週提案", "context": "講者B 同意講者A"}],
        "pending_items": [{"topic": "講者A 的預算數字", "reason": None}],
        "sections": [{"key": "goals", "label": "講者A 目標", "items": ["講者A 想升職", "講者B 支持"]}],
    }


def test_meeting_fields_rename_every_ai_text_but_not_quotes_or_labels():
    f = rename_meeting_fields(meeting_record(), "講者A", "翁曉玲")
    assert f["transcript"] == "[0:01] 翁曉玲：預算太少\n[0:05] 講者B：剛剛講者A說的"
    assert f["meeting"] == {"summary": "翁曉玲質疑預算", "attendees": ["翁曉玲", "講者B"]}
    assert f["highlights"] == [{"text": "翁曉玲質疑預算", "time": "0:01", "source_quote": "講者A：預算太少"}]
    assert f["decisions"] == [{"description": "翁曉玲 下週提案", "context": "講者B 同意翁曉玲"}]
    assert f["pending_items"] == [{"topic": "翁曉玲 的預算數字", "reason": None}]
    assert f["sections"] == [{"key": "goals", "label": "講者A 目標", "items": ["翁曉玲 想升職", "講者B 支持"]}]


def test_meeting_fields_leave_missing_summary_alone():
    """沒產出摘要的種類不寫 summary，免得把 null 變成空字串。"""
    record = meeting_record()
    record["meeting"]["summary"] = None
    del record["highlights"]
    f = rename_meeting_fields(record, "講者A", "翁曉玲")
    assert "summary" not in f["meeting"]
    assert "highlights" not in f


def test_task_fields_rename_owner_task_and_reason_but_not_quote():
    task = {"id": "t1", "task": "講者A 提供報價單", "owner": "講者A",
            "priority_reason": "講者B 催講者A", "source_quote": "講者A：我來"}
    assert rename_task_fields(task, "講者A", "翁曉玲") == {
        "task": "翁曉玲 提供報價單", "owner": "翁曉玲", "priority_reason": "講者B 催翁曉玲",
    }


def test_task_fields_skip_unchanged_values():
    task = {"id": "t1", "task": "整理會議紀錄", "owner": "王小明", "priority_reason": None}
    assert rename_task_fields(task, "講者A", "翁曉玲") == {}


def test_owner_is_only_replaced_on_exact_match():
    """負責人是一個名字，不是自由文字：「講者AB」不是「講者A」。"""
    task = {"id": "t1", "task": "x", "owner": "講者AB"}
    assert rename_task_fields(task, "講者A", "翁曉玲") == {}
