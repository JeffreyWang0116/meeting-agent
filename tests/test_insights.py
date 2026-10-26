"""跨會議追蹤：未決事項、每個人、週報月報。

一場一場看會議紀錄回答不了「那件事後來到底怎麼了」「這個月決定了什麼」。
這裡全部是既有資料的彙整，不打 AI——這類問題的答案應該是確定的，不是
每問一次都換一種說法。
"""
from __future__ import annotations

from app.insights import open_items, people_stats, period_report

M1 = {
    "id": "m1", "meeting": {"title": "第一週", "date": "2026-10-05", "attendees": ["林佳蓉", "陳柏宇"]},
    "decisions": [{"description": "預算維持原案"}],
    "pending_items": [{"topic": "場地", "reason": "還沒問到"},
                      {"topic": "講者邀請", "reason": "名單未定"}],
}
M2 = {
    "id": "m2", "meeting": {"title": "第二週", "date": "2026-10-12", "attendees": ["林佳蓉"]},
    "decisions": [{"description": "場地確定租用 B 棟會議室"}],
    "pending_items": [{"topic": "講者邀請", "reason": "還在等回覆"}],
}
M3 = {
    "id": "m3", "meeting": {"title": "第三週", "date": "2026-10-19", "attendees": ["林佳蓉", "王大明"]},
    "decisions": [],
    "pending_items": [{"topic": "講者邀請", "reason": "有兩位還沒回"}],
}
MEETINGS = [M3, M2, M1]  # store 一律新到舊

TASKS = [
    {"id": "t1", "meeting_id": "m1", "task": "整理預算表", "owner": "林佳蓉",
     "status": "done", "due_date": "2026-10-10"},
    {"id": "t2", "meeting_id": "m1", "task": "訂場地", "owner": "陳柏宇",
     "status": "todo", "due_date": "2026-10-08"},
    {"id": "t3", "meeting_id": "m2", "task": "寄邀請信", "owner": "林佳蓉",
     "status": "todo", "due_date": "2026-11-30"},
    {"id": "t4", "meeting_id": "m3", "task": "沒人認領的事", "owner": None, "status": "todo"},
]
TODAY = "2026-10-26"


# ---- 未決事項看板 ----

def test_the_same_topic_across_meetings_becomes_one_row():
    """「講者邀請」在三場都出現。列成三筆的話，看板就只是把資料再抄一次。"""
    items = {i["topic"]: i for i in open_items(MEETINGS)}

    assert items["講者邀請"]["times"] == 3
    assert [m["id"] for m in items["講者邀請"]["meetings"]] == ["m1", "m2", "m3"]


def test_it_says_how_long_it_has_been_open():
    items = {i["topic"]: i for i in open_items(MEETINGS)}

    assert items["講者邀請"]["first_seen"] == "2026-10-05"
    assert items["講者邀請"]["last_seen"] == "2026-10-19"


def test_a_later_decision_that_mentions_it_is_offered_as_the_answer():
    """「那件事後來怎麼了」最常見的答案就躺在後面某一場的決議裡。"""
    items = {i["topic"]: i for i in open_items(MEETINGS)}

    assert items["場地"]["resolved"] is True
    assert "B 棟會議室" in items["場地"]["resolved_by"]["description"]
    assert items["場地"]["resolved_by"]["meeting_id"] == "m2"


def test_an_earlier_decision_does_not_count_as_the_answer():
    """決議在前、未決在後，那是先後顛倒——不能拿來宣告它解決了。"""
    meetings = [
        {"id": "b", "meeting": {"title": "後", "date": "2026-10-12"},
         "decisions": [], "pending_items": [{"topic": "預算"}]},
        {"id": "a", "meeting": {"title": "前", "date": "2026-10-05"},
         "decisions": [{"description": "預算維持原案"}], "pending_items": []},
    ]

    assert open_items(meetings)[0]["resolved"] is False


def test_unresolved_items_come_first():
    """看板是用來找「還沒解決的」，已經有答案的排在後面。"""
    items = open_items(MEETINGS)

    assert items[0]["topic"] == "講者邀請"
    assert items[0]["resolved"] is False


def test_a_longer_topic_that_contains_a_shorter_one_is_the_same_thing():
    """「場地」與「場地問題」是同一件事，拆成兩列等於沒有彙整。"""
    meetings = [
        {"id": "b", "meeting": {"date": "2026-10-12"}, "decisions": [],
         "pending_items": [{"topic": "場地問題"}]},
        {"id": "a", "meeting": {"date": "2026-10-05"}, "decisions": [],
         "pending_items": [{"topic": "場地"}]},
    ]

    assert len(open_items(meetings)) == 1


def test_very_short_topics_are_not_merged_by_accident():
    """兩個字以下的字串幾乎什麼都包得到，合併只會把不相干的事湊在一起。"""
    meetings = [
        {"id": "b", "meeting": {"date": "2026-10-12"}, "decisions": [],
         "pending_items": [{"topic": "人"}]},
        {"id": "a", "meeting": {"date": "2026-10-05"}, "decisions": [],
         "pending_items": [{"topic": "人力配置"}]},
    ]

    assert len(open_items(meetings)) == 2


def test_no_meetings_no_rows():
    assert open_items([]) == []


# ---- 每個人 ----

def test_each_person_gets_counted():
    stats = {p["name"]: p for p in people_stats(MEETINGS, TASKS, today=TODAY)}

    assert stats["林佳蓉"]["meetings"] == 3
    assert stats["林佳蓉"]["tasks"] == 2
    assert stats["林佳蓉"]["done"] == 1


def test_the_completion_rate_is_a_percentage():
    stats = {p["name"]: p for p in people_stats(MEETINGS, TASKS, today=TODAY)}

    assert stats["林佳蓉"]["rate"] == 50
    assert stats["陳柏宇"]["rate"] == 0


def test_overdue_is_counted_against_today():
    stats = {p["name"]: p for p in people_stats(MEETINGS, TASKS, today=TODAY)}

    assert stats["陳柏宇"]["overdue"] == 1   # 10/08 早就過了
    assert stats["林佳蓉"]["overdue"] == 0   # 11/30 還沒到，10/10 那筆已完成


def test_someone_who_only_owns_tasks_still_appears():
    """只在別場會議被指派、自己沒出席的人也是要追蹤的對象。"""
    stats = {p["name"]: p for p in people_stats([], TASKS, today=TODAY)}

    assert "陳柏宇" in stats
    assert stats["陳柏宇"]["meetings"] == 0


def test_unassigned_tasks_do_not_become_a_person():
    names = [p["name"] for p in people_stats(MEETINGS, TASKS, today=TODAY)]

    assert None not in names
    assert "未指派" not in names


def test_people_with_the_most_open_work_come_first():
    """這份清單是拿來看「誰手上還有事」的。"""
    order = [p["name"] for p in people_stats(MEETINGS, TASKS, today=TODAY)]

    assert order.index("陳柏宇") < order.index("王大明")  # 王大明只出席，沒有任務


def test_the_last_time_we_saw_them():
    stats = {p["name"]: p for p in people_stats(MEETINGS, TASKS, today=TODAY)}

    assert stats["王大明"]["last_seen"] == "2026-10-19"


# ---- 週報月報 ----

def test_a_report_counts_what_happened_in_the_window():
    report = period_report(MEETINGS, TASKS, start="2026-10-12", end="2026-10-26")

    assert report["meetings"] == 2          # 第二週、第三週
    assert report["decisions"] == 1
    assert [m["id"] for m in report["meeting_list"]] == ["m2", "m3"]


def test_the_window_edges_are_included():
    report = period_report(MEETINGS, TASKS, start="2026-10-05", end="2026-10-05")

    assert report["meetings"] == 1


def test_a_report_lists_the_decisions_made():
    report = period_report(MEETINGS, TASKS, start="2026-10-01", end="2026-10-31")

    texts = [d["description"] for d in report["decision_list"]]
    assert "預算維持原案" in texts
    assert "場地確定租用 B 棟會議室" in texts


def test_a_report_counts_tasks_created_in_the_window():
    report = period_report(MEETINGS, TASKS, start="2026-10-12", end="2026-10-26")

    assert report["tasks_created"] == 2  # m2 的 t3、m3 的 t4


def test_a_report_still_works_when_nothing_happened():
    report = period_report(MEETINGS, TASKS, start="2026-01-01", end="2026-01-31")

    assert report["meetings"] == 0
    assert report["decision_list"] == []


def test_a_report_names_who_was_busiest():
    report = period_report(MEETINGS, TASKS, start="2026-10-01", end="2026-10-31")

    assert report["top_owners"][0]["name"] == "林佳蓉"
