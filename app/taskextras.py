"""任務的子項目與重複規則。

子任務讓「整理預算表」這種一句話其實是五件事的任務有地方拆開；重複規則讓
週會的固定事項不必每週手動再打一次——完成一筆就自動排下一筆。
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

# 子任務是放在任務文件裡的一個陣列，不是另一張表：它們永遠跟著那一筆任務
# 讀寫，分開存只會多一次查詢。設上限是為了不讓單一文件無限長大
MAX_SUBTASKS = 50
MAX_SUBTASK_TEXT = 200

RECURRENCES = {
    "daily": "每天",
    "weekly": "每週",
    "biweekly": "每兩週",
    "monthly": "每月",
}


class TaskExtraError(Exception):
    """子任務或重複規則的內容不合法。"""


def clean_subtasks(raw) -> list[dict]:
    """整理成 [{"text", "done"}, ...]。字串與既有物件都收。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TaskExtraError("子任務必須是陣列")
    if len(raw) > MAX_SUBTASKS:
        raise TaskExtraError(f"子任務最多 {MAX_SUBTASKS} 項")
    out = []
    for item in raw:
        if isinstance(item, dict):
            text, done = str(item.get("text") or "").strip(), bool(item.get("done"))
        else:
            text, done = str(item or "").strip(), False
        # 按了「加一項」卻沒打字就送出，不該留下一個點不掉的空白列
        if not text:
            continue
        if len(text) > MAX_SUBTASK_TEXT:
            raise TaskExtraError(f"子任務請控制在 {MAX_SUBTASK_TEXT} 字以內")
        out.append({"text": text, "done": done})
    return out


def subtask_progress(subtasks) -> tuple[int, int]:
    items = subtasks or []
    return sum(1 for s in items if s.get("done")), len(items)


def next_occurrence(due_date: str | None, recurrence: str | None) -> str | None:
    """下一次的期限。算不出來（沒期限、規則不認得、日期壞掉）一律回 None——
    沒有期限的重複任務等於無限產生一堆沒有期限的任務。"""
    if not due_date or recurrence not in RECURRENCES:
        return None
    try:
        current = date.fromisoformat(str(due_date))
    except ValueError:
        return None

    if recurrence == "daily":
        return (current + timedelta(days=1)).isoformat()
    if recurrence == "weekly":
        return (current + timedelta(days=7)).isoformat()
    if recurrence == "biweekly":
        return (current + timedelta(days=14)).isoformat()

    # 每月：保持同一個日期。1/31 的下個月沒有 31 號，壓到當月最後一天——
    # 讓它跑到 3/3 的話，每個月都會再往後漂一點
    year, month = (current.year + 1, 1) if current.month == 12 else (current.year, current.month + 1)
    day = min(current.day, calendar.monthrange(year, month)[1])
    return date(year, month, day).isoformat()
