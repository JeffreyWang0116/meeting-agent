"""群組活動紀錄：誰在什麼時候改了什麼。

群組成員有「可編輯」權限，但在這之前改了什麼完全沒有紀錄——一場會議的
摘要被改掉、一筆任務被刪掉，其他人只會發現東西不一樣了，查不出是誰、
也不知道原本是什麼。
"""
from __future__ import annotations

import pytest

from app.activity import MAX_ENTRIES, actor_name, describe, record
from app.stores.local_store import LocalJsonStore
from tests.test_firestore_store import make_store as make_firestore


@pytest.fixture(params=["local", "firestore"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalJsonStore(tmp_path / "db.json")
    return make_firestore()


def add(store, action="meeting.trash", label="週會", email="amy@example.com", at="2026-10-26T10:00:00Z"):
    return record(store, "group:g1", action=action, target_label=label,
                  actor_uid="uid-amy", actor_email=email, at=at)


# ---- 記錄與讀回 ----

def test_what_was_recorded_reads_back(store):
    add(store)

    entries = store.get_activity("group:g1")
    assert len(entries) == 1
    assert entries[0]["action"] == "meeting.trash"
    assert entries[0]["target_label"] == "週會"
    assert entries[0]["actor_email"] == "amy@example.com"


def test_the_newest_is_first(store):
    add(store, label="第一場", at="2026-10-26T10:00:00Z")
    add(store, label="第二場", at="2026-10-26T11:00:00Z")

    assert [e["target_label"] for e in store.get_activity("group:g1")] == ["第二場", "第一場"]


def test_each_workspace_has_its_own(store):
    add(store)
    record(store, "group:g2", action="meeting.trash", target_label="別組的",
           actor_uid="uid-bob", actor_email="bob@example.com", at="2026-10-26T10:00:00Z")

    assert len(store.get_activity("group:g1")) == 1
    assert store.get_activity("group:g2")[0]["target_label"] == "別組的"


def test_old_entries_fall_off_the_end(store):
    """不設上限的話，這份紀錄會一直長大，而且沒有人會去看三個月前的事。"""
    for i in range(MAX_ENTRIES + 10):
        add(store, label=f"第 {i} 場", at=f"2026-10-26T10:{i:02d}:00Z")

    entries = store.get_activity("group:g1")
    assert len(entries) == MAX_ENTRIES
    assert entries[0]["target_label"] == f"第 {MAX_ENTRIES + 9} 場"  # 最新的留著


def test_personal_workspaces_are_not_logged(store):
    """自己一個人的工作區，每件事都是自己做的，記了只是浪費寫入次數。"""
    record(store, "uid-amy", action="meeting.trash", target_label="我的會議",
           actor_uid="uid-amy", actor_email="amy@example.com", at="2026-10-26T10:00:00Z")

    assert store.get_activity("uid-amy") == []


def test_activity_is_not_part_of_the_backup(store):
    add(store)

    assert "activity" not in store.export_all(user="group:g1")


# ---- 顯示 ----

def test_an_entry_reads_like_a_sentence():
    entry = {"action": "meeting.trash", "target_label": "週會", "actor_email": "amy@example.com"}

    assert describe(entry) == "amy 刪除了會議「週會」"


def test_an_unknown_action_still_says_something():
    """之後新增動作時忘了補字典，畫面不該變成一片空白。"""
    assert describe({"action": "meeting.explode", "target_label": "x", "actor_email": "a@b.c"})


def test_the_name_is_the_part_before_the_at_sign():
    assert actor_name({"actor_email": "amy.chen@example.com", "actor_uid": "uid-amy"}) == "amy.chen"


def test_without_an_email_it_falls_back_to_the_account_id():
    """匿名訪客沒有 email。顯示「某人」的話，兩個訪客就分不出來了。"""
    assert "abc123" in actor_name({"actor_email": None, "actor_uid": "abc123456789"})
