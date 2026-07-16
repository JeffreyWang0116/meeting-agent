"""Reminder Agent：主動提醒模組 — 「主動式」會議 Agent 的核心。

不等使用者開口：掃描任務庫與會議紀錄，找出（1）逾期、（2）即將到期、
（3）沒人負責的任務，以及（4）議而未決的事項，主動擬好催辦／追問草稿。
純規則式、不呼叫 LLM：提醒必須便宜、可靠、可隨時重算。
"""
from __future__ import annotations

from datetime import date

from app.timeutil import today_local


def _parse_date(raw) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def _owner_phrase(task: dict) -> str:
    return task.get("owner") or "（尚無負責人，請先指派）"


def _classify(task: dict, today: date, due_soon_days: int) -> tuple[str, int] | None:
    """回傳 (kind, days)；不需要提醒回傳 None。一個任務最多一種提醒。"""
    due = _parse_date(task.get("due_date"))
    if due is not None:
        delta = (due - today).days
        if delta < 0:
            return "overdue", -delta
        if delta <= due_soon_days:
            return "due_soon", delta
        return None
    if not task.get("owner"):
        return "unassigned", 0
    return None


def _message(kind: str, task: dict, days: int) -> str:
    name = task.get("task", "")
    owner = _owner_phrase(task)
    due = task.get("due_date") or ""
    if kind == "overdue":
        return (
            f"提醒：「{name}」已逾期 {days} 天（原定 {due}），負責人：{owner}。"
            "請回報目前進度，或在系統中更新期限。"
        )
    if kind == "due_soon":
        when = "今天" if days == 0 else f"{days} 天後（{due}）"
        return f"提醒：「{name}」{when}到期，負責人:{owner}。請確認進度是否如期。"
    return f"提醒：「{name}」還沒有負責人，請在下次會議指派或直接於系統中補上。"


_KIND_ORDER = {"overdue": 0, "due_soon": 1, "unassigned": 2}


def scan(
    tasks: list[dict],
    meetings: list[dict],
    today: date | None = None,
    due_soon_days: int = 2,
) -> dict:
    today = today or today_local()

    reminders = []
    for task in tasks:
        if task.get("status") == "done":
            continue
        hit = _classify(task, today, due_soon_days)
        if hit is None:
            continue
        kind, days = hit
        reminders.append(
            {"kind": kind, "days": days, "task": task, "message": _message(kind, task, days)}
        )
    reminders.sort(
        key=lambda r: (
            _KIND_ORDER[r["kind"]],
            -r["days"] if r["kind"] == "overdue" else r["days"],  # 逾期越久越急
        )
    )

    followups = []
    for meeting in meetings:  # list_meetings 已是新到舊
        title = meeting.get("meeting", {}).get("title", "")
        for item in meeting.get("pending_items", []):
            topic = item.get("topic", "")
            reason = item.get("reason")
            message = f"追問：上次會議「{title}」中「{topic}」尚未有結論"
            message += f"（{reason}）。" if reason else "。"
            message += "建議在下次會議排入議程，或先在群組確認。"
            followups.append(
                {
                    "meeting_id": meeting.get("id"),
                    "meeting_title": title,
                    "meeting_date": meeting.get("meeting", {}).get("date"),
                    "topic": topic,
                    "reason": reason,
                    "message": message,
                }
            )

    return {
        "generated_at": today.isoformat(),
        "reminders": reminders,
        "followups": followups[:10],
    }
