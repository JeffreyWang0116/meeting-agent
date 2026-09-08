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
        {"term": "王霖翔", "note": "人名", "person": False},
        {"term": "TaskHub", "note": "", "person": False},
    ]
    # 重新載入（等同重啟服務）要還在——證明有進資料庫
    assert Glossary(LocalJsonStore(path)).terms() == saved


def test_replace_strips_and_dedupes(tmp_path):
    g = make_glossary(tmp_path)
    saved = g.replace([
        {"term": "  王霖翔 ", "note": None},
        {"term": "王霖翔", "note": "重複的會被跳過"},
    ])
    assert saved == [{"term": "王霖翔", "note": "", "person": False}]


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


# ---- 人名合併：講者名冊歸入詞彙表 ----

def test_clean_terms_keeps_the_person_flag():
    """標成人名的詞彙同時有兩個用途：餵轉錄（別聽錯字）＋餵講者命名（寫法一致）。"""
    from app.glossary import clean_terms

    out = clean_terms([{"term": "王霖翔", "person": True}, {"term": "TaskHub"}])
    assert out[0]["person"] is True
    assert out[1]["person"] is False


def test_person_names_only_returns_people(tmp_path):
    from app.glossary import Glossary
    from app.stores.local_store import LocalJsonStore

    g = Glossary(LocalJsonStore(tmp_path / "db.json"))
    g.replace([
        {"term": "TaskHub", "note": "產品名"},
        {"term": "王霖翔", "person": True},
        {"term": "李四", "person": True},
    ])
    assert g.person_names() == ["王霖翔", "李四"]


def test_remember_persons_adds_new_names_without_touching_curated_terms(tmp_path):
    """AI 命名成功時自動記下姓名。這是分析流程的副作用，絕不能拋例外，
    也不能擠掉使用者手動整理的詞彙。"""
    from app.glossary import Glossary
    from app.stores.local_store import LocalJsonStore

    g = Glossary(LocalJsonStore(tmp_path / "db.json"))
    g.replace([{"term": "TaskHub", "note": "產品名"}])
    g.remember_persons(["王霖翔", "王霖翔", "講者A", ""])

    terms = g.terms()
    assert [t["term"] for t in terms] == ["TaskHub", "王霖翔"]
    assert g.person_names() == ["王霖翔"]  # 代號與空字串被擋掉


def test_remember_persons_never_raises(tmp_path):
    from app.glossary import Glossary
    from app.stores.local_store import LocalJsonStore

    g = Glossary(LocalJsonStore(tmp_path / "db.json"))
    g.remember_persons(None)          # 型別亂給也不能炸
    g.remember_persons(["x" * 500])   # 過長的姓名略過即可
    assert g.person_names() == []


def test_legacy_roster_is_migrated_into_the_glossary(tmp_path):
    """舊版把講者名冊存在另一個地方。合併後要自動搬過來，
    否則使用者的名冊會像憑空消失。"""
    from app.glossary import Glossary
    from app.stores.local_store import LocalJsonStore

    store = LocalJsonStore(tmp_path / "db.json")
    store.save_glossary([{"term": "TaskHub", "note": "產品名"}])
    store.save_speaker_roster(["王霖翔", "李四"])

    g = Glossary(store)
    assert g.person_names() == ["王霖翔", "李四"]
    assert store.get_speaker_roster() == []  # 搬完清空，不會再搬第二次
