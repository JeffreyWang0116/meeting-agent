"""自訂詞彙表：驗證、prompt 片段產生，以及透過 store 持久化。"""
import pytest

from app.glossary import Glossary, glossary_prompt_line, terms_hint_line
from app.stores.base import DEFAULT_USER
from app.stores.local_store import LocalJsonStore

# user 必填（見 Glossary 的說明），測試也照 production 的形狀明講是誰
U = "uid-test"


def make_glossary(tmp_path):
    return Glossary(LocalJsonStore(tmp_path / "db.json"))


def test_empty_glossary(tmp_path):
    assert make_glossary(tmp_path).terms(U) == []


def test_replace_and_reload(tmp_path):
    path = tmp_path / "db.json"
    g = Glossary(LocalJsonStore(path))
    saved = g.replace([{"term": "林佳蓉", "note": "人名"}, {"term": "TaskHub", "note": ""}], U)
    assert saved == [
        {"term": "林佳蓉", "note": "人名"},
        {"term": "TaskHub", "note": ""},
    ]
    # 重新載入（等同重啟服務）要還在——證明有進資料庫
    assert Glossary(LocalJsonStore(path)).terms(U) == saved


def test_replace_strips_and_dedupes(tmp_path):
    g = make_glossary(tmp_path)
    saved = g.replace([
        {"term": "  林佳蓉 ", "note": None},
        {"term": "林佳蓉", "note": "重複的會被跳過"},
    ], U)
    assert saved == [{"term": "林佳蓉", "note": ""}]


def test_empty_term_rejected(tmp_path):
    g = make_glossary(tmp_path)
    with pytest.raises(ValueError):
        g.replace([{"term": "   "}], U)


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


# ---- 「人名」勾選已移除：系統不再自動對應講者姓名，這個標記沒有任何用途 ----

def test_person_flag_is_dropped_on_save_and_on_load(tmp_path):
    """舊前端或舊備份還會帶 person，存進來與讀出去都不該再出現。"""
    from app.glossary import clean_terms

    assert clean_terms([{"term": "林佳蓉", "person": True}]) == [{"term": "林佳蓉", "note": ""}]

    store = LocalJsonStore(tmp_path / "db.json")
    store.save_glossary([{"term": "林佳蓉", "note": "", "person": True}])
    assert Glossary(store).terms(DEFAULT_USER) == [{"term": "林佳蓉", "note": ""}]


def test_legacy_roster_is_migrated_into_the_glossary(tmp_path):
    """舊版把講者名冊存在另一個地方。搬進詞彙表當一般詞彙（註明人名），
    姓名照樣幫轉錄聽對字，使用者的名冊也不會像憑空消失。"""
    from app.glossary import Glossary
    from app.stores.local_store import LocalJsonStore

    store = LocalJsonStore(tmp_path / "db.json")
    store.save_glossary([{"term": "TaskHub", "note": "產品名"}])
    store.save_speaker_roster(["林佳蓉", "李四"])

    g = Glossary(store)
    assert g.terms(DEFAULT_USER) == [
        {"term": "TaskHub", "note": "產品名"},
        {"term": "林佳蓉", "note": "人名"},
        {"term": "李四", "note": "人名"},
    ]
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
        if not name.startswith("_") and name not in {"terms", "replace"}
    ]
    assert writers == [], f"Glossary 多了 replace 以外的寫入口：{writers}"


def test_terms_are_isolated_per_account(tmp_path):
    """A 設的詞彙不能出現在 B 的詞彙表或 prompt 片段裡。"""
    g = make_glossary(tmp_path)
    g.replace([{"term": "TaskHub", "note": "產品"}], user="uid-A")

    assert g.terms(user="uid-B") == []
    assert glossary_prompt_line(g.terms(user="uid-B")) == ""
    assert terms_hint_line(g.terms(user="uid-B")) == ""
    assert g.terms(user="uid-A")[0]["term"] == "TaskHub"


def test_user_is_required_so_forgetting_it_fails_loudly(tmp_path):
    """Glossary 的兩個對外方法都不給 user 預設值。

    這是「詞彙表讀到別人那一桶」那個 bug 的根本原因：user 有預設值
    （DEFAULT_USER）時，少傳一個參數不會報錯，而是安靜地讀到另一個人的資料
    ——四條 prompt 路徑同時中招，壞了一個月沒有任何徵兆。

    回傳值會直接進 prompt 的這一層，寧可在呼叫端就 TypeError：CI 當場擋下來，
    好過變成線上的跨帳號資料外洩。
    """
    g = make_glossary(tmp_path)
    for call in (lambda: g.terms(), lambda: g.replace([])):
        with pytest.raises(TypeError):
            call()
