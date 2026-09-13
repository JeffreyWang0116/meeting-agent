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


def test_remember_persons_adds_new_names_without_touching_curated_terms(tmp_path):
    """AI 命名成功時自動記下姓名。這是分析流程的副作用，絕不能拋例外，
    也不能擠掉使用者手動整理的詞彙。"""
    from app.glossary import Glossary
    from app.stores.local_store import LocalJsonStore

    g = Glossary(LocalJsonStore(tmp_path / "db.json"))
    g.replace([{"term": "TaskHub", "note": "產品名"}])
    g.remember_persons(["林佳蓉", "林佳蓉", "講者A", ""])

    terms = g.terms()
    assert [t["term"] for t in terms] == ["TaskHub", "林佳蓉"]
    assert g.person_names() == ["林佳蓉"]  # 代號與空字串被擋掉


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
    store.save_speaker_roster(["林佳蓉", "李四"])

    g = Glossary(store)
    assert g.person_names() == ["林佳蓉", "李四"]
    assert store.get_speaker_roster() == []  # 搬完清空，不會再搬第二次


# ---- 詞彙表只收使用者自己輸入的詞 ----

def test_ai_naming_is_not_wired_to_write_the_glossary():
    """AI 認出的姓名不得自動寫進詞彙表。

    自動記憶會形成迴圈：某場會議認出一個姓名 → 寫進詞彙表 → 之後每一場的
    轉錄與分析 prompt 都帶著它 → 模型把它套到不相干的講者身上 → 又被記住
    一次。使用者從沒在詞彙表輸入過那個名字，卻場場都看到它出現，而且完全
    查不出是哪來的。

    SpeakerNamerAgent 的 remember_names 掛鉤本身保留（/api/glossary/persons
    那條「使用者手動改講者名」的路徑要用），這裡釘的是 create_app 不再把它
    接到 AI 命名上。接線在 create_app 內部、從 app 物件取不到，所以比照
    test_frontend_modules.py 的做法做靜態檢查。
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent / "app" / "main.py").read_text(
        encoding="utf-8"
    )
    assert "remember_names=" not in src, "create_app 又把 AI 命名接回自動寫入詞彙表了"


def test_terms_are_isolated_per_account(tmp_path):
    """A 設的詞彙不能出現在 B 的詞彙表或 prompt 片段裡。"""
    g = make_glossary(tmp_path)
    g.replace([{"term": "TaskHub", "note": "產品"}], user="uid-A")

    assert g.terms(user="uid-B") == []
    assert glossary_prompt_line(g.terms(user="uid-B")) == ""
    assert terms_hint_line(g.terms(user="uid-B")) == ""
    assert g.person_names(user="uid-B") == []
    assert g.terms(user="uid-A")[0]["term"] == "TaskHub"
