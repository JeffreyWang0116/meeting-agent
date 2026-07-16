"""自訂詞彙表：驗證、prompt 片段產生，以及透過 store 持久化。"""
import pytest

from app.glossary import Glossary, glossary_prompt_line
from app.stores.local_store import LocalJsonStore


def make_glossary(tmp_path):
    return Glossary(LocalJsonStore(tmp_path / "db.json"))


def test_empty_glossary(tmp_path):
    assert make_glossary(tmp_path).terms() == []


def test_replace_and_reload(tmp_path):
    path = tmp_path / "db.json"
    g = Glossary(LocalJsonStore(path))
    saved = g.replace([{"term": "王霖翔", "note": "人名"}, {"term": "TaskHub", "note": ""}])
    assert saved == [
        {"term": "王霖翔", "note": "人名"},
        {"term": "TaskHub", "note": ""},
    ]
    # 重新載入（等同重啟服務）要還在——證明有進資料庫
    assert Glossary(LocalJsonStore(path)).terms() == saved


def test_replace_strips_and_dedupes(tmp_path):
    g = make_glossary(tmp_path)
    saved = g.replace([
        {"term": "  王霖翔 ", "note": None},
        {"term": "王霖翔", "note": "重複的會被跳過"},
    ])
    assert saved == [{"term": "王霖翔", "note": ""}]


def test_empty_term_rejected(tmp_path):
    g = make_glossary(tmp_path)
    with pytest.raises(ValueError):
        g.replace([{"term": "   "}])


def test_prompt_line_formats_terms_with_notes():
    line = glossary_prompt_line(
        [{"term": "王霖翔", "note": "人名"}, {"term": "TaskHub", "note": ""}]
    )
    assert line == "王霖翔（人名）、TaskHub"
    assert glossary_prompt_line([]) == ""
