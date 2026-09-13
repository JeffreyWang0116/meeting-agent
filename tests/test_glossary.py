"""自訂詞彙表：驗證、prompt 片段產生，以及透過 store 持久化。"""
import pytest

from app.glossary import Glossary, glossary_prompt_line, terms_hint_line
from app.stores.local_store import LocalJsonStore


def make_glossary(tmp_path):
    return Glossary(LocalJsonStore(tmp_path / "db.json"))


def test_empty_glossary(tmp_path):
    assert make_glossary(tmp_path).terms() == []


def test_replace_and_reload(tmp_path):
    path = tmp_path / "db.json"
    g = Glossary(LocalJsonStore(path))
    saved = g.replace([{"term": "林佳蓉", "note": "人名"}, {"term": "TaskHub", "note": ""}])
    assert saved == [
        {"term": "林佳蓉", "note": "人名", "person": False},
        {"term": "TaskHub", "note": "", "person": False},
    ]
    # 重新載入（等同重啟服務）要還在——證明有進資料庫
    assert Glossary(LocalJsonStore(path)).terms() == saved


def test_replace_strips_and_dedupes(tmp_path):
    g = make_glossary(tmp_path)
    saved = g.replace([
        {"term": "  林佳蓉 ", "note": None},
        {"term": "林佳蓉", "note": "重複的會被跳過"},
    ])
    assert saved == [{"term": "林佳蓉", "note": "", "person": False}]


def test_empty_term_rejected(tmp_path):
    g = make_glossary(tmp_path)
    with pytest.raises(ValueError):
        g.replace([{"term": "   "}])


def test_prompt_line_formats_terms_with_notes():
    line = glossary_prompt_line(
        [{"term": "林佳蓉", "note": "人名"}, {"term": "TaskHub", "note": ""}]
    )
    assert line == "林佳蓉（人名）、TaskHub"
    assert glossary_prompt_line([]) == ""


def test_terms_hint_line_is_a_full_sentence_for_the_transcriber():
    """轉錄用的詞彙提示。措辭與 GeminiTranscriber.build_prompt 裡那句一致，
    因為兩邊講的是同一件事——只是一個走全域詞彙表、一個走本次專用詞彙。"""
    line = terms_hint_line([{"term": "Kessel 專案", "note": ""}])
    assert "Kessel 專案" in line
    assert "相近發音" in line
    # 詞彙表不可以影響講者標籤：轉錄階段一律輸出代號
    assert "代號" in line


def test_terms_hint_line_is_empty_without_terms():
    """沒有詞彙就不要往 prompt 塞空句子——多一句廢話就多一分干擾。"""
    assert terms_hint_line([]) == ""
    assert terms_hint_line(None) == ""


# ---- 人名合併：講者名冊歸入詞彙表 ----

def test_clean_terms_keeps_the_person_flag():
    """標成人名的詞彙同時有兩個用途：餵轉錄（別聽錯字）＋餵講者命名（寫法一致）。"""
    from app.glossary import clean_terms

    out = clean_terms([{"term": "林佳蓉", "person": True}, {"term": "TaskHub"}])
    assert out[0]["person"] is True
    assert out[1]["person"] is False


def test_person_names_only_returns_people(tmp_path):
    from app.glossary import Glossary
    from app.stores.local_store import LocalJsonStore

    g = Glossary(LocalJsonStore(tmp_path / "db.json"))
    g.replace([
        {"term": "TaskHub", "note": "產品名"},
        {"term": "林佳蓉", "person": True},
        {"term": "李四", "person": True},
    ])
    assert g.person_names() == ["林佳蓉", "李四"]


def test_legacy_roster_is_migrated_into_the_glossary(tmp_path):
    """舊版把講者名冊存在另一個地方。合併後要自動搬過來，
    否則使用者的名冊會像憑空消失。"""
    from app.glossary import Glossary
    from app.stores.local_store import LocalJsonStore

    store = LocalJsonStore(tmp_path / "db.json")
    store.save_glossary([{"term": "TaskHub", "note": "產品名"}])
    store.save_speaker_roster(["林佳蓉", "李四"])

    g = Glossary(store)
    assert g.person_names() == ["林佳蓉", "李四"]
    assert store.get_speaker_roster() == []  # 搬完清空，不會再搬第二次


# ---- 詞彙表只收使用者自己輸入的詞 ----

def test_replace_is_the_only_way_into_the_glossary():
    """詞彙表只有一個寫入口：使用者在管理介面按下的整份取代。

    曾經有兩條路徑會在使用者背後把姓名塞進來——AI 命名成功時自動記住，以及
    在歷史會議手動改講者名時順手記一筆。兩者都會形成迴圈：寫進去之後每一場
    的轉錄與分析 prompt 都帶著它，模型於是把它套到不相干的講者身上，然後又
    被記住一次。使用者從沒在詞彙表輸入過那個名字，卻場場都看到，而且完全查
    不出是哪來的。

    兩條都已整條移除。這裡釘的是「不要再長回來」：Glossary 對外只能有
    replace 這一個寫入方法。
    """
    from app.glossary import Glossary

    writers = [
        name for name in dir(Glossary)
        if not name.startswith("_") and name not in {"terms", "person_names", "replace"}
    ]
    assert writers == [], f"Glossary 多了 replace 以外的寫入口：{writers}"


def test_terms_are_isolated_per_account(tmp_path):
    """A 設的詞彙不能出現在 B 的詞彙表或 prompt 片段裡。"""
    g = make_glossary(tmp_path)
    g.replace([{"term": "TaskHub", "note": "產品"}], user="uid-A")

    assert g.terms(user="uid-B") == []
    assert glossary_prompt_line(g.terms(user="uid-B")) == ""
    assert terms_hint_line(g.terms(user="uid-B")) == ""
    assert g.person_names(user="uid-B") == []
    assert g.terms(user="uid-A")[0]["term"] == "TaskHub"
