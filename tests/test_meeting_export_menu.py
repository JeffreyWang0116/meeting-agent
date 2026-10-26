"""歷史會議的「分享／匯出」選單（app/static/js/meetings.js）。

原本分散三處：列上的「下載」、展開後的「分享」與「加入行事曆」——一場會議要
匯出得先記得每個按鈕在哪一層。收成列上一個選單，展開後不再重複出現。
靜態檢查，不需要 node。
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "meetings.js").read_text(
    encoding="utf-8"
)


def _function_body(name: str) -> str:
    start = SRC.index(f"function {name}(")
    nxt = re.compile(r"^(?:async )?function |^\$\(|^let |^const ", re.M).search(SRC, start + 1)
    return SRC[start: nxt.start() if nxt else len(SRC)]


def test_row_has_one_menu_with_every_export():
    row = _function_body("renderMeetings")
    menu = row[row.index("export-menu"):]
    for needed in ("share-detail", "/report.md", "/events.ics"):
        assert needed in menu, f"選單裡少了 {needed}"
    assert "export-toggle" in row


def test_expanded_detail_no_longer_repeats_share_and_calendar():
    detail = _function_body("meetingDetailHtml")
    assert "share-detail" not in detail
    assert "events.ics" not in detail


def test_share_works_without_expanding_the_meeting_first():
    """選單在列上，按分享時那場會議可能還沒展開過、快取裡沒有內容。"""
    handler = SRC[SRC.index('closest(".share-detail")'):]
    handler = handler[: handler.index("return;")]
    assert "api.getMeeting" in handler
