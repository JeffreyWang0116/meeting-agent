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
