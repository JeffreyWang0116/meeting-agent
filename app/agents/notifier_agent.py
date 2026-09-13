"""Notifier Agent：時程同步與通知模組。

產生「會議結論確認信草稿」與「行事曆事件 JSON」並存到本地。

calendar_events 的格式就是 Google Calendar API events.insert 的請求主體，
前端（static/js/calendar.js）拿著使用者授權的 token 逐筆送出。信件那邊還是
只產草稿，由使用者自己在 Gmail／預設信箱按下寄出。
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import timedelta
from pathlib import Path

from app.models import MeetingAnalysis

_PRIORITY_ZH = {"high": "高", "medium": "中", "low": "低"}


def event_id(meeting_id: str, task: str) -> str:
    """同一筆代辦永遠算出同一個 id，讓「加入行事曆」可以按第二次。

    Google 的 events.insert 接受自帶 id，重複插入同一個 id 會回 409 —— 前端
    就靠這個分辨「已經加過」而不是再塞一份重複的。id 的字元集是 base32hex
    （a-v、0-9），所以不能直接用中文任務名，要先雜湊過。

    注意 Google 的 id 在事件被刪除後仍會被佔用，使用者手動從行事曆刪掉那筆，
    再按一次加入會一直得到 409、加不回來。
    """
    digest = hashlib.sha1(f"{meeting_id}\x00{task}".encode()).digest()
    return "ma" + base64.b32hexencode(digest).decode().rstrip("=").lower()


def build_email_subject(analysis: MeetingAnalysis) -> str:
    """信件主旨。與草稿全文分開回傳，前端才能直接填進 Gmail/mailto 的主旨欄，
    不必反過來剖析草稿第一行。"""
    m = analysis.meeting
    return f"【會議紀錄確認】{m.title}（{m.date}）"


def build_email_draft(analysis: MeetingAnalysis) -> str:
    m = analysis.meeting
    lines = [
        f"主旨：{build_email_subject(analysis)}",
        "",
        "各位好，",
        "",
        f"以下是 {m.date}「{m.title}」的會議結論整理，請協助確認內容是否正確，如有錯漏請直接回覆此信。",
        "",
        "■ 出席者",
        "、".join(m.attendees) if m.attendees else "（未識別）",
        "",
        "■ 會議摘要",
        m.summary or "（未產生摘要）",
        "",
        "■ 決議事項",
    ]
    if analysis.decisions:
        for i, d in enumerate(analysis.decisions, 1):
            suffix = f"（{d.context}）" if d.context else ""
            lines.append(f"{i}. {d.description}{suffix}")
    else:
        lines.append("（本次會議無正式決議）")

    lines += ["", "■ 代辦事項"]
    if analysis.todos:
        for i, t in enumerate(analysis.todos, 1):
            owner = t.owner or "未指派"
            due = str(t.due_date) if t.due_date else "未定"
            lines.append(
                f"{i}. {t.task}｜負責人：{owner}｜期限：{due}｜優先級：{_PRIORITY_ZH[t.priority]}"
            )
    else:
        lines.append("（無）")

    lines += ["", "■ 待確認事項"]
    if analysis.pending_items:
        for i, p in enumerate(analysis.pending_items, 1):
            suffix = f"（{p.reason}）" if p.reason else ""
            lines.append(f"{i}. {p.topic}{suffix}")
    else:
        lines.append("（無）")

    lines += ["", "— 此信由會議助手自動產生"]
    return "\n".join(lines)


def build_calendar_events(analysis: MeetingAnalysis, meeting_id: str = "") -> list[dict]:
    """產生 Google Calendar API events.insert 可直接使用的全天事件。"""
    events = []
    for t in analysis.todos:
        if t.due_date is None:
            continue
        description_parts = [
            f"負責人：{t.owner or '未指派'}",
            f"優先級：{_PRIORITY_ZH[t.priority]}",
            f"會議：{analysis.meeting.title}（{analysis.meeting.date}）",
        ]
        if t.source_quote:
            description_parts.append(f"出處：{t.source_quote}")
        events.append(
            {
                "id": event_id(meeting_id, t.task),
                "summary": f"【代辦】{t.task}",
                "description": "\n".join(description_parts),
                "start": {"date": t.due_date.isoformat()},
                # Google 全天事件的 end.date 是 exclusive，要填隔天
                "end": {"date": (t.due_date + timedelta(days=1)).isoformat()},
            }
        )
    return events


class NotifierAgent:
    def __init__(self, output_dir: Path | str):
        self.output_dir = Path(output_dir)

    def notify(self, meeting_id: str, analysis: MeetingAnalysis) -> dict:
        target = self.output_dir / meeting_id
        target.mkdir(parents=True, exist_ok=True)

        email_draft = build_email_draft(analysis)
        events = build_calendar_events(analysis, meeting_id)

        email_path = target / "email_draft.txt"
        events_path = target / "calendar_events.json"
        email_path.write_text(email_draft, encoding="utf-8")
        events_path.write_text(
            json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        return {
            "email_draft": email_draft,
            "email_subject": build_email_subject(analysis),
            "email_draft_path": str(email_path),
            "calendar_events": events,
            "calendar_events_path": str(events_path),
        }
