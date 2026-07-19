"""Notifier Agent：時程同步與通知模組。

產生「會議結論確認信草稿」與「行事曆事件 JSON」並存到本地。
9 月串接真實 API 時，只需把這裡產生的內容交給 Gmail API 寄出、
把 calendar_events 逐筆丟給 Google Calendar API 的 events.insert，
內容產生邏輯不用重寫。
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from app.models import MeetingAnalysis

_PRIORITY_ZH = {"high": "高", "medium": "中", "low": "低"}


def build_email_draft(analysis: MeetingAnalysis) -> str:
    m = analysis.meeting
    lines = [
        f"主旨：【會議紀錄確認】{m.title}（{m.date}）",
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

    lines += ["", "— 此信由主動式會議 Agent 自動產生"]
    return "\n".join(lines)


def build_calendar_events(analysis: MeetingAnalysis) -> list[dict]:
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
        events = build_calendar_events(analysis)

        email_path = target / "email_draft.txt"
        events_path = target / "calendar_events.json"
        email_path.write_text(email_draft, encoding="utf-8")
        events_path.write_text(
            json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        return {
            "email_draft": email_draft,
            "email_draft_path": str(email_path),
            "calendar_events": events,
            "calendar_events_path": str(events_path),
        }
