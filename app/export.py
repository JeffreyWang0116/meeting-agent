"""匯出：任務 CSV（Excel 可直開）、會議紀錄 Markdown 與行事曆 .ics。"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone

_PRIORITY_ZH = {"high": "高", "medium": "中", "low": "低"}
_STATUS_ZH = {"todo": "待辦", "doing": "進行中", "done": "完成"}


def tasks_to_csv(tasks: list[dict]) -> str:
    """回傳含 BOM 的 CSV 字串（BOM 讓 Excel 正確以 UTF-8 開啟中文）。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["任務", "負責人", "期限", "優先級", "狀態", "來源會議", "來源句"])
    for t in tasks:
        writer.writerow([
            t.get("task", ""),
            t.get("owner") or "未指派",
            t.get("due_date") or "",
            _PRIORITY_ZH.get(t.get("priority"), t.get("priority", "")),
            _STATUS_ZH.get(t.get("status"), t.get("status", "")),
            t.get("meeting_id", ""),
            t.get("source_quote") or "",
        ])
    return "﻿" + buf.getvalue()


def _ics_escape(text: str) -> str:
    """RFC 5545 文字跳脫：反斜線、分號、逗號與換行。"""
    return (
        text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    )


def tasks_to_ics(meeting_title: str, tasks: list[dict]) -> str:
    """把含期限的任務轉成 .ics 全天事件，可直接匯入 Google/Apple 行事曆。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//meeting-agent//ZH-TW",
        "CALSCALE:GREGORIAN",
    ]
    for t in tasks:
        due = t.get("due_date")
        if not due:
            continue
        d = date.fromisoformat(str(due))
        description = f"負責人：{t.get('owner') or '未指派'}｜會議：{meeting_title}"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{t.get('id', '')}@meeting-agent",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
            # 全天事件的 DTEND 是 exclusive，要填隔天
            f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_ics_escape('【代辦】' + (t.get('task') or ''))}",
            f"DESCRIPTION:{_ics_escape(description)}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def meeting_report_md(meeting_record: dict, tasks: list[dict]) -> str:
    """把一場會議的分析結果組成可分享的 Markdown 會議紀錄。"""
    m = meeting_record["meeting"]
    lines = [
        f"# {m['title']}",
        "",
        f"- 日期：{m['date']}",
        f"- 出席：{'、'.join(m['attendees']) or '（未記錄）'}",
        "",
        "## 摘要",
        "",
        m["summary"],
        "",
        "## 決議",
        "",
    ]
    decisions = meeting_record.get("decisions", [])
    lines += [
        f"- {d['description']}" + (f"（{d['context']}）" if d.get("context") else "")
        for d in decisions
    ] or ["（本次會議無正式決議）"]

    lines += ["", "## 代辦事項", ""]
    if tasks:
        lines += [
            "| 任務 | 負責人 | 期限 | 優先級 | 狀態 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for t in tasks:
            lines.append(
                f"| {t.get('task', '')} | {t.get('owner') or '未指派'} | {t.get('due_date') or '未定'} "
                f"| {_PRIORITY_ZH.get(t.get('priority'), '')} | {_STATUS_ZH.get(t.get('status'), '')} |"
            )
    else:
        lines.append("（無代辦事項）")

    pending = meeting_record.get("pending_items", [])
    if pending:
        lines += ["", "## 待決事項", ""]
        lines += [
            f"- {p['topic']}" + (f"（{p['reason']}）" if p.get("reason") else "")
            for p in pending
        ]
    return "\n".join(lines) + "\n"
