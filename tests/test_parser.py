"""輸入文字正規化測試。"""
import pytest

from app.orchestrator import normalize_text


def test_empty_input_rejected():
    with pytest.raises(ValueError):
        normalize_text("")
    with pytest.raises(ValueError):
        normalize_text("   \n  \t ")


def test_windows_line_endings_normalized():
    assert normalize_text("第一行\r\n第二行") == "第一行\n第二行"


def test_excess_blank_lines_collapsed():
    text = "A 說要開會\n\n\n\n\nB 說好"
    assert normalize_text(text) == "A 說要開會\n\nB 說好"


def test_zero_width_and_bom_removed():
    text = "﻿會議​紀錄"
    assert normalize_text(text) == "會議紀錄"


def test_trailing_spaces_stripped_per_line():
    assert normalize_text("哈囉   \n世界\t\t") == "哈囉\n世界"


def test_mixed_language_content_preserved():
    text = "Kevin: 我們用 FastAPI 好了\n志明: ok 那 deadline 訂下週五"
    assert normalize_text(text) == text
