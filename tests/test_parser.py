"""Parser Agent：輸入文字正規化測試。"""
import pytest

from app.agents.parser_agent import ParserAgent


@pytest.fixture
def parser():
    return ParserAgent()


def test_empty_input_rejected(parser):
    with pytest.raises(ValueError):
        parser.parse("")
    with pytest.raises(ValueError):
        parser.parse("   \n  \t ")


def test_windows_line_endings_normalized(parser):
    assert parser.parse("第一行\r\n第二行") == "第一行\n第二行"


def test_excess_blank_lines_collapsed(parser):
    text = "A 說要開會\n\n\n\n\nB 說好"
    assert parser.parse(text) == "A 說要開會\n\nB 說好"


def test_zero_width_and_bom_removed(parser):
    text = "﻿會議​紀錄"
    assert parser.parse(text) == "會議紀錄"


def test_trailing_spaces_stripped_per_line(parser):
    assert parser.parse("哈囉   \n世界\t\t") == "哈囉\n世界"


def test_mixed_language_content_preserved(parser):
    text = "Kevin: 我們用 FastAPI 好了\n鈺翔: ok 那 deadline 訂下週五"
    assert parser.parse(text) == text
