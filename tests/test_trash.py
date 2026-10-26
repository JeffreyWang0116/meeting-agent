"""垃圾桶：刪除會議先進回收區，30 天內救得回來。

群組裡任何一個「可編輯」的成員誤刪一場會議，影響的是所有人，而且在這之前
完全沒有復原的可能——分析結果、逐字稿、任務一次全沒。
"""
from __future__ import annotations

import pytest

from app.models import MeetingAnalysis
from app.stores.local_store import LocalJsonStore
from tests.test_firestore_store import make_store as make_firestore


@pytest.fixture(params=["local", "firestore"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalJsonStore(tmp_path / "db.json")
    return make_firestore()


def analysis(title="週會"):
    return MeetingAnalysis.model_validate({
        "meeting": {"title": title, "date": "2026-10-26", "summary": "摘要", "attendees": ["甲"]},
        "todos": [{"task": "交報告", "owner": "甲", "due_date": "2026-10-30", "priority": "high"}],
        "decisions": [], "pending_items": [], "highlights": [], "sections": [], "tags": [],
    })


def saved(store, title="週會", user="uid-A"):
    return store.save_meeting(analysis(title), transcript="[0:01] 甲：哈囉", user=user)


# ---- 丟進垃圾桶 ----

def test_a_trashed_meeting_leaves_the_list(store):
    mid = saved(store)

    assert store.trash_meeting(mid, "2026-10-26T10:00:00Z", user="uid-A") is True

    assert store.list_meetings(user="uid-A") == []
    assert store.get_meeting(mid, user="uid-A") is None


def test_its_tasks_leave_the_task_list_too(store):
    """任務留著的話，提醒還是會叫你去做一場已經刪掉的會議裡的事。"""
    mid = saved(store)
    assert len(store.list_tasks(user="uid-A")) == 1

    store.trash_meeting(mid, "2026-10-26T10:00:00Z", user="uid-A")

    assert store.list_tasks(user="uid-A") == []


def test_the_trash_shows_what_is_in_it(store):
    mid = saved(store, "被刪掉的週會")
    store.trash_meeting(mid, "2026-10-26T10:00:00Z", user="uid-A")

    items = store.list_trash("uid-A")

    assert len(items) == 1
    assert items[0]["id"] == mid
    assert items[0]["deleted_at"] == "2026-10-26T10:00:00Z"
    assert items[0]["meeting"]["title"] == "被刪掉的週會"


def test_another_workspace_cannot_trash_it(store):
    mid = saved(store, user="group:g1")

    assert store.trash_meeting(mid, "2026-10-26T10:00:00Z", user="uid-A") is False
    assert len(store.list_meetings(user="group:g1")) == 1


def test_the_trash_is_per_workspace(store):
    a = saved(store, "我的", "uid-A")
    b = saved(store, "群組的", "group:g1")
    store.trash_meeting(a, "2026-10-26T10:00:00Z", user="uid-A")
    store.trash_meeting(b, "2026-10-26T10:00:00Z", user="group:g1")

    assert [m["id"] for m in store.list_trash("uid-A")] == [a]
    assert [m["id"] for m in store.list_trash("group:g1")] == [b]


# ---- 救回來 ----

def test_restoring_brings_back_the_meeting_and_its_tasks(store):
    mid = saved(store)
    store.trash_meeting(mid, "2026-10-26T10:00:00Z", user="uid-A")

    assert store.restore_meeting(mid, user="uid-A") is True

    assert len(store.list_meetings(user="uid-A")) == 1
    assert len(store.list_tasks(user="uid-A")) == 1
    assert store.list_trash("uid-A") == []


def test_the_transcript_survives_a_round_trip(store):
    """救回來只剩標題的話，等於沒救回來。"""
    mid = saved(store)
    store.trash_meeting(mid, "2026-10-26T10:00:00Z", user="uid-A")
    store.restore_meeting(mid, user="uid-A")

    assert store.get_meeting(mid, user="uid-A")["transcript"] == "[0:01] 甲：哈囉"


def test_restoring_something_that_is_not_in_the_trash_says_so(store):
    assert store.restore_meeting("nope", user="uid-A") is False


# ---- 永久刪除 ----

def test_permanently_deleting_from_the_trash(store):
    mid = saved(store)
    store.trash_meeting(mid, "2026-10-26T10:00:00Z", user="uid-A")

    assert store.delete_meeting(mid, user="uid-A") is True

    assert store.list_trash("uid-A") == []
    assert store.restore_meeting(mid, user="uid-A") is False


def test_deleting_a_meeting_that_was_never_trashed_still_works(store):
    """purge_scope（刪帳號、解散群組）走的是這條路，不該被垃圾桶擋住。"""
    mid = saved(store)

    assert store.delete_meeting(mid, user="uid-A") is True
    assert store.list_meetings(user="uid-A") == []
    assert store.list_trash("uid-A") == []


def test_trashed_meetings_are_not_in_the_backup(store):
    """備份是「我的資料」，不是「我刪掉的東西」。"""
    mid = saved(store)
    store.trash_meeting(mid, "2026-10-26T10:00:00Z", user="uid-A")

    data = store.export_all(user="uid-A")

    assert data["meetings"] == []
    assert data["tasks"] == []
