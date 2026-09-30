"""詢問會議的條件解析：把「上個月跟客戶那場」變成日期範圍、種類、人名，再在本地篩會議。"""
import json
from datetime import date

from app.ask_planner import AskPlan, build_plan_prompt, filter_meetings, parse_plan

KINDS = ["一般會議", "銷售拜訪", "面試"]
TODAY = date(2026, 9, 30)


def raw(**fields) -> str:
    return json.dumps(fields, ensure_ascii=False)


# ---- 提示 ----

def test_prompt_carries_today_kinds_and_question():
    prompt = build_plan_prompt("上個月跟客戶開的會？", TODAY, KINDS)
    assert "2026-09-30" in prompt
    assert "銷售拜訪" in prompt
    assert "上個月跟客戶開的會？" in prompt


# ---- 解析 ----

def test_parse_full_plan():
    plan = parse_plan(
        raw(date_from="2026-08-01", date_to="2026-08-31", kinds=["銷售拜訪"],
            people=["王小明"], query="報價與預算", keywords=["報價", "預算"]),
        "上個月跟王小明的客戶會議談了什麼報價？", KINDS,
    )
    assert plan == AskPlan(
        date_from=date(2026, 8, 1), date_to=date(2026, 8, 31), kinds=["銷售拜訪"],
        people=["王小明"], query="報價與預算", keywords=["報價", "預算"],
    )
    assert plan.has_filters


def test_parse_accepts_code_fence_and_nulls():
    plan = parse_plan("```json\n" + raw(date_from=None, kinds=None, query="預算") + "\n```", "預算？", KINDS)
    assert plan.date_from is None and plan.kinds == [] and plan.query == "預算"
    assert not plan.has_filters


def test_unknown_kinds_are_dropped():
    """模型自創的種類（「客戶會議」）對不到任何會議，留著只會篩成零場。"""
    plan = parse_plan(raw(kinds=["客戶會議", "面試"], query="x"), "x", KINDS)
    assert plan.kinds == ["面試"]


def test_reversed_dates_are_swapped():
    plan = parse_plan(raw(date_from="2026-08-31", date_to="2026-08-01", query="x"), "x", KINDS)
    assert (plan.date_from, plan.date_to) == (date(2026, 8, 1), date(2026, 8, 31))


def test_broken_output_falls_back_to_the_question():
    for bad in ["不是 JSON", raw(date_from="上個月"), "", None]:
        plan = parse_plan(bad, "預算誰負責？", KINDS)
        assert plan == AskPlan(query="預算誰負責？"), bad


def test_empty_query_falls_back_to_the_question_and_keywords_are_cleaned():
    plan = parse_plan(raw(query="  ", keywords=["預算", " ", "預算", "報價"], people=[" 王小明 ", ""]), "預算？", KINDS)
    assert plan.query == "預算？"
    assert plan.keywords == ["預算", "報價"]
    assert plan.people == ["王小明"]


# ---- 篩選 ----

def meeting(mid, day, kind="一般會議", attendees=(), summary="", transcript=""):
    return {"id": mid, "kind": kind, "transcript": transcript,
            "meeting": {"date": day, "attendees": list(attendees), "summary": summary}}


MEETINGS = [
    meeting("aug-sales", "2026-08-12", "銷售拜訪", ["王小明", "講者B"]),
    meeting("sep-general", "2026-09-03", transcript="[0:01] 講者A：李華下週交報告"),
    meeting("jul-legacy", "2026-07-20", "訪談"),  # 改版前的舊種類值
    meeting("no-date", None),
]


def ids(plan, tasks=()):
    return filter_meetings(MEETINGS, list(tasks), plan)


def test_no_filters_keeps_every_meeting():
    assert ids(AskPlan(query="x")) == ["aug-sales", "sep-general", "jul-legacy", "no-date"]


def test_date_range_is_inclusive_and_skips_undated_meetings():
    plan = AskPlan(query="x", date_from=date(2026, 8, 12), date_to=date(2026, 9, 3))
    assert ids(plan) == ["aug-sales", "sep-general"]
    assert ids(AskPlan(query="x", date_from=date(2026, 9, 1))) == ["sep-general"]


def test_kind_filter_maps_legacy_kinds():
    assert ids(AskPlan(query="x", kinds=["銷售拜訪"])) == ["aug-sales"]
    kinds = ["需求訪談"]
    assert filter_meetings(MEETINGS, [], AskPlan(query="x", kinds=kinds)) == ["jul-legacy"]


def test_person_matches_attendees_owners_and_transcript():
    assert ids(AskPlan(query="x", people=["王小明"])) == ["aug-sales"]
    assert ids(AskPlan(query="x", people=["李華"])) == ["sep-general"]  # 逐字稿內文提到
    tasks = [{"meeting_id": "jul-legacy", "owner": "陳美玲"}]
    assert ids(AskPlan(query="x", people=["陳美玲"]), tasks) == ["jul-legacy"]


def test_person_matches_shorter_attendee_name():
    """出席者常只寫「小明」，問的是「王小明」。"""
    ms = [meeting("m", "2026-09-01", attendees=["小明"])]
    assert filter_meetings(ms, [], AskPlan(query="x", people=["王小明"])) == ["m"]


def test_every_person_must_match():
    assert ids(AskPlan(query="x", people=["王小明", "李華"])) == []


def test_single_character_attendee_does_not_match_everyone():
    ms = [meeting("m", "2026-09-01", attendees=["王"])]
    assert filter_meetings(ms, [], AskPlan(query="x", people=["王小明"])) == []
