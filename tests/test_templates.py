"""自訂會議範本：使用者自己定義種類名稱、要哪些區塊、每個區塊抓什麼。

內建的八種是通用的，但學生的「專題 meeting」、社團的「幹部會議」各有自己想看的
欄位。Section 本來就是「標籤＋清單」的通用形狀，所以輸出端（前端、Markdown
匯出、儲存層）一行都不用改，只要分析時把區塊定義餵進提示詞。

範本存在工作區底下（user 欄位），所以群組的範本群組成員自動共用——與會議、
任務、詞彙表同一套隔離方式。
"""
from __future__ import annotations

import pytest

from app.stores.local_store import LocalJsonStore
from app.templates import TemplateError, normalize_template
from tests.test_firestore_store import make_store as make_firestore


@pytest.fixture(params=["local", "firestore"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalJsonStore(tmp_path / "db.json")
    return make_firestore()


def tpl(tid="tp1", user="uid-A", **extra):
    return {
        "id": tid, "user": user, "name": "專題 meeting",
        "hint": "學校專題的每週進度會議。",
        "sections": [{"label": "本週進度", "desc": "這週做完的事"},
                     {"label": "卡住的地方", "desc": "遇到的問題"}],
        "features": ["summary", "todos"],
        **extra,
    }


# ---- 儲存 ----

def test_saved_template_reads_back(store):
    store.save_template(tpl())

    got = store.list_templates("uid-A")
    assert len(got) == 1
    assert got[0]["name"] == "專題 meeting"
    assert [s["label"] for s in got[0]["sections"]] == ["本週進度", "卡住的地方"]


def test_templates_are_per_workspace(store):
    store.save_template(tpl("tp1", "uid-A"))
    store.save_template(tpl("tp2", "group:g1"))

    assert [t["id"] for t in store.list_templates("uid-A")] == ["tp1"]
    assert [t["id"] for t in store.list_templates("group:g1")] == ["tp2"]


def test_get_and_delete(store):
    store.save_template(tpl())

    assert store.get_template("tp1", "uid-A")["name"] == "專題 meeting"
    store.delete_template("tp1", "uid-A")
    assert store.get_template("tp1", "uid-A") is None


def test_cannot_touch_another_workspaces_template(store):
    store.save_template(tpl("tp1", "group:g1"))

    assert store.get_template("tp1", "uid-A") is None
    store.delete_template("tp1", "uid-A")
    assert store.get_template("tp1", "group:g1") is not None


def test_templates_are_not_part_of_the_backup(store):
    """備份是會議與任務。還原備份不該把範本清掉——那會讓所有舊會議的種類失效。"""
    store.save_template(tpl())

    assert "templates" not in store.export_all(user="uid-A")
    store.import_all({"meetings": [], "tasks": []}, user="uid-A")
    assert len(store.list_templates("uid-A")) == 1


# ---- 驗證 ----

def test_name_is_required():
    with pytest.raises(TemplateError):
        normalize_template({"name": "  ", "sections": []})


def test_sections_need_labels():
    with pytest.raises(TemplateError):
        normalize_template({"name": "x", "sections": [{"label": "", "desc": "y"}]})


def test_duplicate_section_labels_are_rejected():
    """label 是輸出的 key，重複的話前端會畫出兩個一樣的區塊。"""
    with pytest.raises(TemplateError):
        normalize_template({"name": "x", "sections": [{"label": "A"}, {"label": "A"}]})


def test_too_many_sections_are_rejected():
    """每個區塊都要進提示詞，不設上限等於讓人塞爆 token。"""
    with pytest.raises(TemplateError):
        normalize_template({"name": "x", "sections": [{"label": f"S{i}"} for i in range(21)]})


def test_unknown_features_are_rejected():
    with pytest.raises(TemplateError):
        normalize_template({"name": "x", "sections": [], "features": ["不存在的功能"]})


def test_normalized_template_trims_and_defaults():
    out = normalize_template({"name": "  專題  ", "sections": [{"label": " 進度 "}]})

    assert out["name"] == "專題"
    assert out["sections"] == [{"label": "進度", "desc": ""}]
    assert out["hint"] == ""
    assert set(out["features"]) == {"summary", "decisions", "todos", "highlights"}


def test_a_template_with_no_sections_is_fine():
    """只想換個提示語、不加欄位也算一種範本。"""
    out = normalize_template({"name": "讀書會", "hint": "輕鬆記錄就好", "sections": []})

    assert out["sections"] == []
