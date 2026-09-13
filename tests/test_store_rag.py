"""RAG 索引的持久化：兩個 store 後端行為必須一致。

原本索引固定寫本地 JSON 檔，雲端免費方案沒有持久磁碟，每次部署就整份消失
——跨會議問答得把所有會議重新向量化，既慢又吃 embedding 額度。改成交給
store：本地仍是 JSON 檔，雲端走 Firestore，與會議／任務／詞彙表同一後端。

Firestore 單一文件上限 1MB，而一筆記錄光向量就好幾 KB，整份索引塞不進一個
文件，所以一筆記錄一個文件。這裡釘住的是「兩個後端對外行為一致」，索引本身
的檢索邏輯在 test_rag.py。
"""
from __future__ import annotations

import pytest

from app.stores.local_store import LocalJsonStore
from tests.test_firestore_store import FakeFirestore


@pytest.fixture(params=["local", "firestore"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalJsonStore(tmp_path / "db.json")
    from app.stores.firestore_store import FirestoreStore

    return FirestoreStore(FakeFirestore())


def _rec(meeting_id: str, text: str, vector: list[float], user: str = "uid-A") -> dict:
    return {
        "meeting_id": meeting_id,
        "user": user,
        "title": "專題會議",
        "date": "2026-09-14",
        "text": text,
        "vector": vector,
    }


def test_empty_index_reads_back_as_empty(store):
    assert store.get_rag_records() == {"dim": None, "records": []}


def test_records_and_dim_round_trip(store):
    records = [_rec("m1", "第一段", [0.1, 0.2]), _rec("m1", "第二段", [0.3, 0.4])]
    store.save_rag_records(768, records)

    got = store.get_rag_records()
    assert got["dim"] == 768
    assert got["records"] == records


def test_saving_fewer_records_drops_the_removed_ones(store):
    """會議被編輯或刪除時，索引會少掉那場的片段——舊的不能留下來。"""
    store.save_rag_records(768, [_rec("m1", "甲", [0.1]), _rec("m2", "乙", [0.2])])
    store.save_rag_records(768, [_rec("m2", "乙", [0.2])])

    got = store.get_rag_records()
    assert [r["text"] for r in got["records"]] == ["乙"]


def test_reindexing_a_meeting_replaces_its_content(store):
    """會議編輯後重新索引：同一場會議、同樣的片段數，但內容換了。

    Firestore 後端用「會議 id + 序號」當文件 id，若只憑 id 存在就跳過寫入，
    這種情況會留著舊向量——查詢時撈到的還是編輯前的內容。
    """
    store.save_rag_records(768, [_rec("m1", "舊內容", [0.1])])
    store.save_rag_records(768, [])                      # drop_meeting
    store.save_rag_records(768, [_rec("m1", "新內容", [0.9])])

    got = store.get_rag_records()
    assert [r["text"] for r in got["records"]] == ["新內容"]
    assert got["records"][0]["vector"] == [0.9]


def test_index_survives_a_fresh_store_instance(tmp_path):
    """重啟服務（或雲端重新部署）之後索引還在——這正是搬到 store 的目的。"""
    path = tmp_path / "db.json"
    LocalJsonStore(path).save_rag_records(768, [_rec("m1", "甲", [0.1])])

    got = LocalJsonStore(path).get_rag_records()
    assert got["dim"] == 768
    assert [r["text"] for r in got["records"]] == ["甲"]


def test_many_records_are_not_crammed_into_one_firestore_document():
    """一筆記錄一個文件：整份索引塞進單一文件會撞上 Firestore 的 1MB 上限。"""
    from app.stores.firestore_store import FirestoreStore

    db = FakeFirestore()
    store = FirestoreStore(db)
    store.save_rag_records(768, [_rec("m1", f"片段{i}", [float(i)]) for i in range(30)])

    assert len(store.get_rag_records()["records"]) == 30
