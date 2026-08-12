"""匯出（Markdown 會議報告）測試。"""
from app.export import meeting_report_md
from tests.test_models import make_valid_payload


def make_record():
    payload = make_valid_payload()
    return {"meeting": payload["meeting"], "decisions": payload["decisions"]}


def test_meeting_report_md_contains_summary():
    md = meeting_report_md(make_record(), tasks=[])
    assert "討論 7 月里程碑進度與分工。" in md


def test_meeting_report_md_handles_missing_summary():
    """summary 功能沒被使用（None）時，報告不能出現 Python 的 "None" 字樣。"""
    record = make_record()
    record["meeting"]["summary"] = None
    md = meeting_report_md(record, tasks=[])
    assert "None" not in md


def test_meeting_report_md_includes_highlights_with_time():
    record = make_record()
    record["highlights"] = make_valid_payload()["highlights"]
    md = meeting_report_md(record, tasks=[])
    assert "## 會議重點" in md
    assert "1. 決定後端採用 FastAPI（1:02）" in md


def test_meeting_report_md_omits_highlights_section_when_empty():
    md = meeting_report_md(make_record(), tasks=[])
    assert "會議重點" not in md


def test_report_includes_kind_specific_sections():
    """種類專屬區塊（BANT、事件時間軸…）也要進 Markdown 報告，
    不然下載下來的檔案跟畫面上看到的不一樣。"""
    record = {
        "id": "m1",
        "meeting": {"title": "客戶拜訪", "date": "2026-08-10", "attendees": [], "summary": None},
        "decisions": [],
        "pending_items": [],
        "sections": [
            {"label": "預算", "items": ["年度預算 50 萬"]},
            {"label": "時程", "items": []},
        ],
    }
    md = meeting_report_md(record, [])
    assert "## 預算" in md
    assert "- 年度預算 50 萬" in md
    assert "## 時程" in md
    assert "（本次未提及）" in md
