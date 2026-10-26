"""跨會議追蹤：未決事項看板、每個人的狀況、週報月報。

一場一場翻會議紀錄回答不了「那件事後來到底怎麼了」「這個月決定了什麼」
「誰手上還有事」。這三個問題都只是既有資料的彙整，所以這裡完全不打 AI——
這類答案應該是確定的，不是每問一次都換一種說法。
"""
from __future__ import annotations

import re

# 單字幾乎什麼都包得到（「人」會吃掉「人力配置」），合併只會把不相干的事
# 湊在一起。中文兩個字已經是一個詞（「場地」vs「場地問題」確實是同一件事），
# 所以門檻訂在 2
MIN_MERGE_LENGTH = 2
TOP_OWNERS = 5

_NOISE = re.compile(r"[\s　,，、。.：:；;（）()\[\]「」【】的之]")


def _key(text: str) -> str:
    return _NOISE.sub("", str(text or ""))


def _date_of(meeting: dict) -> str:
    return (meeting.get("meeting") or {}).get("date") or ""


def _oldest_first(meetings: list[dict]) -> list[dict]:
    """store 一律新到舊，但「這件事是什麼時候第一次提的」要從舊往新看。"""
    return sorted(meetings or [], key=_date_of)


def open_items(meetings: list[dict]) -> list[dict]:
    """把所有會議的待確認事項彙整成一列一件事。

    同一件事在不同場次常常寫法不同（「場地」「場地問題」），所以用去掉標點
    之後的包含關係判斷是不是同一件；太短的字串不參與合併。
    """
    ordered = _oldest_first(meetings)
    groups: list[dict] = []
    for meeting in ordered:
        for item in meeting.get("pending_items") or []:
            topic = str(item.get("topic") or "").strip()
            if not topic:
                continue
            key = _key(topic)
            found = next(
                (g for g in groups if _same_topic(g["key"], key)), None
            )
            if found is None:
                found = {"topic": topic, "key": key, "meetings": [], "reasons": []}
                groups.append(found)
            elif len(topic) > len(found["topic"]):
                found["topic"] = topic  # 寫得比較完整的那個版本比較好讀
                found["key"] = key
            found["meetings"].append({
                "id": meeting.get("id"),
                "title": (meeting.get("meeting") or {}).get("title"),
                "date": _date_of(meeting),
            })
            if item.get("reason"):
                found["reasons"].append(item["reason"])

    out = []
    for g in groups:
        last_seen = g["meetings"][-1]["date"]
        resolved_by = _resolution(g["key"], ordered, after=last_seen)
        out.append({
            "topic": g["topic"],
            "times": len(g["meetings"]),
            "meetings": g["meetings"],
            "first_seen": g["meetings"][0]["date"],
            "last_seen": last_seen,
            "reason": g["reasons"][-1] if g["reasons"] else None,
            "resolved": resolved_by is not None,
            "resolved_by": resolved_by,
        })
    # 看板是用來找「還沒解決的」，已經有答案的排在後面；同組內久的在前
    out.sort(key=lambda i: (i["resolved"], i["first_seen"]))
    return out


def _same_topic(a: str, b: str) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) < MIN_MERGE_LENGTH:
        return False
    return a in b or b in a


def _resolution(key: str, ordered: list[dict], after: str) -> dict | None:
    """這個主題最後一次被提出來之後，有沒有哪一場的決議講到它。

    只是提示而不是定論：文字對得上不代表真的拍板了。但「那件事後來怎麼了」
    的答案，絕大多數就躺在後面某一場的決議裡，找出來比讓人自己翻有用得多。
    決議在前、未決在後的不算——那是先後顛倒。
    """
    for meeting in ordered:
        if _date_of(meeting) < after:
            continue
        for decision in meeting.get("decisions") or []:
            text = str(decision.get("description") or "")
            if key and key in _key(text):
                return {
                    "description": text,
                    "meeting_id": meeting.get("id"),
                    "title": (meeting.get("meeting") or {}).get("title"),
                    "date": _date_of(meeting),
                }
    return None


def people_stats(meetings: list[dict], tasks: list[dict], today: str) -> list[dict]:
    """每個人出席幾場、手上幾件事、完成率、逾期幾件。

    只在別場會議被指派、自己沒出席的人也要出現——那正是最容易漏掉的人。
    """
    people: dict[str, dict] = {}

    def entry(name: str) -> dict:
        return people.setdefault(name, {
            "name": name, "meetings": 0, "tasks": 0, "done": 0, "overdue": 0, "last_seen": "",
        })

    for meeting in meetings or []:
        for name in (meeting.get("meeting") or {}).get("attendees") or []:
            name = str(name).strip()
            if not name:
                continue
            p = entry(name)
            p["meetings"] += 1
            p["last_seen"] = max(p["last_seen"], _date_of(meeting))

    for task in tasks or []:
        owner = str(task.get("owner") or "").strip()
        if not owner:
            continue  # 沒人認領的不是一個人
        p = entry(owner)
        p["tasks"] += 1
        if task.get("status") == "done":
            p["done"] += 1
        elif task.get("due_date") and str(task["due_date"]) < today:
            p["overdue"] += 1

    out = []
    for p in people.values():
        p["open"] = p["tasks"] - p["done"]
        p["rate"] = round(p["done"] / p["tasks"] * 100) if p["tasks"] else 0
        out.append(p)
    # 這份清單是拿來看「誰手上還有事」的
    out.sort(key=lambda p: (-p["overdue"], -p["open"], -p["tasks"], p["name"]))
    return out


def period_report(meetings: list[dict], tasks: list[dict], start: str, end: str) -> dict:
    """一段期間內發生了什麼。週報就是七天，月報就是一個月，差別只有範圍。"""
    in_window = [m for m in _oldest_first(meetings) if start <= _date_of(m) <= end]
    ids = {m.get("id") for m in in_window}
    window_tasks = [t for t in tasks or [] if t.get("meeting_id") in ids]

    decisions = [
        {"description": d.get("description"), "meeting_id": m.get("id"),
         "title": (m.get("meeting") or {}).get("title"), "date": _date_of(m)}
        for m in in_window for d in m.get("decisions") or []
    ]
    owners: dict[str, int] = {}
    for t in window_tasks:
        owner = str(t.get("owner") or "").strip()
        if owner:
            owners[owner] = owners.get(owner, 0) + 1

    return {
        "start": start,
        "end": end,
        "meetings": len(in_window),
        "meeting_list": [
            {"id": m.get("id"), "title": (m.get("meeting") or {}).get("title"), "date": _date_of(m)}
            for m in in_window
        ],
        "decisions": len(decisions),
        "decision_list": decisions,
        "tasks_created": len(window_tasks),
        "tasks_done": sum(1 for t in window_tasks if t.get("status") == "done"),
        "top_owners": [
            {"name": name, "tasks": count}
            for name, count in sorted(owners.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_OWNERS]
        ],
        "open_items": [i for i in open_items(in_window) if not i["resolved"]],
    }
