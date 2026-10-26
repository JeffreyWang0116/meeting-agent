"""會議紀錄匯出成 Word（.docx）與 PDF。

Markdown 與 CSV 對工程師很好用，但要寄給老師、客戶或主管的會議紀錄，對方
期待的是打得開、印得出來的檔案。

中文是這兩種格式最容易出事的地方：PDF 沒有指定中文字型的話，整份會變成
空白方框，而且在開發機上（有系統字型）常常看起來是對的。
"""
from __future__ import annotations

import io
import zipfile

import pytest

from app.docexport import meeting_to_docx, meeting_to_pdf

RECORD = {
    "meeting": {"title": "專題進度會議", "date": "2026-10-26",
                "summary": "討論了預算與期程，決定維持原案。", "attendees": ["林佳蓉", "陳柏宇"]},
    "decisions": [{"description": "預算維持原案", "context": "三比一通過"}],
    "pending_items": [{"topic": "場地", "reason": "還沒問到"}],
    "highlights": [{"text": "談到預算", "time": "0:01"}],
    "sections": [{"label": "風險", "items": ["時程可能延後"]}],
    "transcript": "[0:01] 林佳蓉：今天要談預算",
}
TASKS = [
    {"id": "t1", "task": "整理預算表", "owner": "林佳蓉", "due_date": "2026-10-30",
     "priority": "high", "status": "todo"},
]


# ---- Word ----

def docx_text(blob: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        return z.read("word/document.xml").decode("utf-8")


def test_a_docx_is_a_zip_that_word_recognises():
    blob = meeting_to_docx(RECORD, TASKS)

    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = z.namelist()
    assert "[Content_Types].xml" in names
    assert "word/document.xml" in names


def test_the_docx_contains_the_meeting_content():
    xml = docx_text(meeting_to_docx(RECORD, TASKS))

    assert "專題進度會議" in xml
    assert "討論了預算與期程" in xml
    assert "預算維持原案" in xml
    assert "整理預算表" in xml


def test_custom_sections_make_it_into_the_docx():
    """自訂範本的區塊是那種會議真正要看的東西，漏掉等於匯出了半份紀錄。"""
    xml = docx_text(meeting_to_docx(RECORD, TASKS))

    assert "風險" in xml
    assert "時程可能延後" in xml


def test_a_meeting_with_nothing_in_it_still_produces_a_file():
    blob = meeting_to_docx({"meeting": {"title": "空會議", "date": "2026-10-26", "attendees": []}}, [])

    assert "空會議" in docx_text(blob)


def test_the_docx_can_be_read_back_by_a_word_reader():
    """自己寫出來自己讀得懂不算數，要能被獨立的 OOXML 讀取器解析。"""
    docx = pytest.importorskip("docx")

    doc = docx.Document(io.BytesIO(meeting_to_docx(RECORD, TASKS)))

    assert doc.paragraphs[0].text == "專題進度會議"
    assert any("預算維持原案" in p.text for p in doc.paragraphs)


# ---- PDF ----

def test_a_pdf_starts_with_the_pdf_header():
    blob = meeting_to_pdf(RECORD, TASKS)

    assert blob.startswith(b"%PDF-")
    assert len(blob) > 1000


def test_the_chinese_in_the_pdf_is_the_chinese_we_put_in():
    """這一條是用來擋真的發生過的事：用 reportlab 內建的 CID 字型
    （MSung-Light）時，版面完全正確、每一個字卻都是別的字——「專題進度會議」
    畫出來是「菡斌砦東滘琡」。因為那種字型不把字型資料內嵌進 PDF，檢視器
    只好拿手邊的字型去對 CID。

    「有沒有中文」或「檔案開得起來」都過得了那個 bug，只有把字讀回來才擋得住。
    """
    pypdf = pytest.importorskip("pypdf")

    reader = pypdf.PdfReader(io.BytesIO(meeting_to_pdf(RECORD, TASKS)))
    text = "".join(page.extract_text() for page in reader.pages)

    assert "專題進度會議" in text
    assert "討論了預算與期程" in text
    assert "預算維持原案" in text
    assert "整理預算表" in text


def test_the_pdf_carries_its_own_font():
    """內嵌（FontFile2）是上面那條能成立的前提。對方的電腦不會剛好有你的字型。"""
    blob = meeting_to_pdf(RECORD, TASKS)

    assert b"FontFile2" in blob


def test_a_machine_with_no_chinese_font_says_so_instead_of_producing_garbage():
    from app.docexport import PdfFontMissing, find_cjk_font

    with pytest.raises(PdfFontMissing):
        find_cjk_font(paths=("/nowhere/no-such-font.ttf",))


def test_a_font_file_reportlab_cannot_read_does_not_count_as_found():
    """真的發生過：fonts-noto-cjk 路徑存在、檔案也正常，但它是 OpenType/CFF
    外框，reportlab 的 TTFont 只讀得懂 TrueType 的 glyf。只檢查路徑在不在的話，
    會在使用者按下「下載 PDF」的當下才炸。"""
    from app.docexport import PdfFontMissing, find_cjk_font

    with pytest.raises(PdfFontMissing):
        find_cjk_font(paths=(__file__,))  # 存在，但不是字型


def test_a_working_font_is_still_found_after_a_broken_one():
    """壞掉的那個不該讓整串候選就此中斷。"""
    from app.docexport import find_cjk_font

    path, index = find_cjk_font()  # 這台機器上真的有的那一個
    found, _ = find_cjk_font(paths=(__file__, "/nowhere/nope.ttf", path))

    assert found == path


def test_a_long_meeting_spills_onto_more_than_one_page():
    record = {**RECORD, "decisions": [{"description": f"第 {i} 條決議" * 5} for i in range(120)]}

    blob = meeting_to_pdf(record, TASKS)

    assert blob.count(b"/Type /Page\n") > 1 or blob.count(b"/Type/Page") > 1


def test_an_empty_meeting_still_produces_a_pdf():
    blob = meeting_to_pdf({"meeting": {"title": "空會議", "date": "2026-10-26", "attendees": []}}, [])

    assert blob.startswith(b"%PDF-")
