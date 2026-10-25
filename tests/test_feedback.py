"""回報 AI 的錯誤：哪一筆、錯在哪、正確答案是什麼。

目的不是改那一筆（要改有「請 AI 修改」和就地編輯），而是累積真實的錯誤案例。
換模型前（例如要升 gemini-3.8-flash）就有自己的資料可以回歸測試，而不是只靠
手寫的 eval/dataset.jsonl。

回報只存在自己的帳號，不傳給任何外部服務。
"""
from __future__ import annotations

import pytest

from app.stores.local_store import LocalJsonStore
from tests.test_firestore_store import make_store as make_firestore


@pytest.fixture(params=["local", "firestore"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalJsonStore(tmp_path / "db.json")
    return make_firestore()


def item(fid="f1", user="uid-A", **extra):
    return {
        "id": fid, "user": user, "meeting_id": "m1", "created_at": "2026-10-26T10:00:00Z",
        "target": "todo", "target_text": "整理預算表",
        "reason": "owner_wrong", "note": "應該是小華不是講者A",
        **extra,
    }


def test_saved_feedback_reads_back(store):
    store.save_feedback(item())

    assert store.list_feedback("uid-A")[0]["note"] == "應該是小華不是講者A"


def test_feedback_is_per_user(store):
    store.save_feedback(item("f1", "uid-A"))
    store.save_feedback(item("f2", "uid-B"))

    assert [f["id"] for f in store.list_feedback("uid-A")] == ["f1"]


def test_deleting_one(store):
    store.save_feedback(item())
    store.delete_feedback("f1", "uid-A")

    assert store.list_feedback("uid-A") == []


def test_cannot_delete_someone_elses(store):
    store.save_feedback(item("f1", "uid-B"))

    store.delete_feedback("f1", "uid-A")

    assert len(store.list_feedback("uid-B")) == 1


def test_feedback_is_not_part_of_the_backup(store):
    """備份是會議與任務；回報是另一回事，還原備份不該把它清掉。"""
    store.save_feedback(item())

    assert "feedback" not in store.export_all(user="uid-A")
    store.import_all({"meetings": [], "tasks": []}, user="uid-A")
    assert len(store.list_feedback("uid-A")) == 1
