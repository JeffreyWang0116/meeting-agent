"""群組活動紀錄：誰在什麼時候改了什麼。

群組成員有「可編輯」權限，但在這之前改了什麼完全沒有紀錄——摘要被改掉、
任務被刪掉，其他人只會發現東西不一樣了，查不出是誰動的。

整份紀錄存成工作區底下的一個文件（同詞彙表、講者名冊），不是一筆一個文件：
免費層 Firestore 每天只有 2 萬次寫入，而這是「每個動作都要寫一次」的東西。
一次讀、一次寫、長度封頂，成本與事件數脫鉤。
"""
from __future__ import annotations

# 保留幾筆。不設上限的話這份紀錄會一直長大，而且沒有人會去看三個月前的事
MAX_ENTRIES = 300

ACTIONS = {
    "meeting.create": "新增了會議",
    "meeting.edit": "編輯了會議",
    "meeting.reanalyze": "重新分析了會議",
    "meeting.trash": "刪除了會議",
    "meeting.restore": "從垃圾桶救回了會議",
    "meeting.purge": "永久刪除了會議",
    "task.add": "新增了任務",
    "task.edit": "修改了任務",
    "task.done": "完成了任務",
    "task.reopen": "把任務改回未完成",
    "task.delete": "刪除了任務",
    "comment.add": "留言在",
}


def is_group(user: str) -> bool:
    return str(user).startswith("group:")


def actor_name(entry: dict) -> str:
    """顯示用的人名。成員紀錄裡只有 email，沒有暱稱（見 app/groups.py）。"""
    email = entry.get("actor_email") or ""
    if "@" in email:
        return email.split("@", 1)[0]
    # 匿名訪客沒有 email。寫「某人」的話兩個訪客就分不出來了
    uid = entry.get("actor_uid") or ""
    return f"訪客 {uid[:6]}" if uid else "某人"


def describe(entry: dict) -> str:
    verb = ACTIONS.get(entry.get("action", ""), "改了")
    label = entry.get("target_label") or ""
    return f"{actor_name(entry)} {verb}「{label}」" if label else f"{actor_name(entry)} {verb}"


def record(store, user: str, *, action: str, target_label: str, actor_uid: str,
           actor_email: str | None, at: str, target_id: str | None = None) -> None:
    """記一筆。個人工作區不記——每件事都是自己做的，記了只是浪費寫入次數。"""
    if not is_group(user):
        return
    entry = {
        "at": at,
        "action": action,
        "target_label": target_label,
        "target_id": target_id,
        "actor_uid": actor_uid,
        "actor_email": actor_email,
    }
    store.save_activity(user, [entry, *store.get_activity(user)][:MAX_ENTRIES])
