"""留言的位置驗證與儲存。"""
from __future__ import annotations

import pytest

from app.comments import CommentError, author_name, clean_text, normalize_target
from app.stores.local_store import LocalJsonStore
from tests.test_firestore_store import make_store as make_firestore


@pytest.fixture(params=["local", "firestore"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalJsonStore(tmp_path / "db.json")
    return make_firestore()


def comment(cid="c1", user="group:g1", meeting_id="m1", target="summary", **extra):
    return {
        "id": cid, "user": user, "meeting_id": meeting_id, "target": target,
        "text": "這條其實還沒定案", "author_uid": "uid-amy", "author_email": "amy@example.com",
        "at": "2026-10-26T10:00:00Z", **extra,
    }


# ---- 位置 ----

def test_the_simple_places():
    assert normalize_target("summary") == "summary"


def test_indexed_places_keep_their_number():
    assert normalize_target("decision:2") == "decision:2"


def test_a_leading_zero_is_normalised_away():
    """前端算出 02 與 2 時，不該變成掛在兩個不同地方的留言。"""
    assert normalize_target("decision:02") == "decision:2"


def test_a_task_is_identified_by_its_id():
    assert normalize_target("task:abc123") == "task:abc123"


def test_a_custom_section_keeps_its_label():
    assert normalize_target("section:風險") == "section:風險"


def test_an_index_that_is_not_a_number_is_rejected():
    with pytest.raises(CommentError):
        normalize_target("decision:第二條")


def test_an_unknown_place_is_rejected():
    """放行任意字串的話，前端寫錯一個字就是一筆永遠顯示不出來的留言。"""
    with pytest.raises(CommentError):
        normalize_target("whatever:1")


def test_an_empty_place_is_rejected():
    with pytest.raises(CommentError):
        normalize_target("  ")


# ---- 內容 ----

def test_text_is_trimmed():
    assert clean_text("  有話要說  ") == "有話要說"


def test_empty_text_is_rejected():
    with pytest.raises(CommentError):
        clean_text("   ")


def test_absurdly_long_text_is_rejected():
    with pytest.raises(CommentError):
        clean_text("字" * 2001)


def test_the_name_is_the_part_before_the_at_sign():
    assert author_name({"author_email": "amy@example.com"}) == "amy"


# ---- 儲存 ----

def test_a_saved_comment_reads_back(store):
    store.save_comment(comment())

    got = store.list_comments("group:g1")
    assert len(got) == 1
    assert got[0]["text"] == "這條其實還沒定案"


def test_comments_can_be_fetched_for_one_meeting(store):
    store.save_comment(comment("c1", meeting_id="m1"))
    store.save_comment(comment("c2", meeting_id="m2"))

    assert [c["id"] for c in store.list_comments("group:g1", meeting_id="m1")] == ["c1"]


def test_comments_are_per_workspace(store):
    store.save_comment(comment("c1", user="group:g1"))
    store.save_comment(comment("c2", user="uid-amy"))

    assert [c["id"] for c in store.list_comments("group:g1")] == ["c1"]


def test_oldest_first_so_a_thread_reads_top_to_bottom(store):
    store.save_comment(comment("c2", at="2026-10-26T11:00:00Z"))
    store.save_comment(comment("c1", at="2026-10-26T10:00:00Z"))

    assert [c["id"] for c in store.list_comments("group:g1")] == ["c1", "c2"]


def test_deleting_one(store):
    store.save_comment(comment())
    store.delete_comment("c1", "group:g1")

    assert store.list_comments("group:g1") == []


def test_cannot_delete_another_workspaces_comment(store):
    store.save_comment(comment("c1", user="group:g1"))

    store.delete_comment("c1", "uid-amy")

    assert len(store.list_comments("group:g1")) == 1


def test_comments_are_not_part_of_the_backup(store):
    store.save_comment(comment())

    assert "comments" not in store.export_all(user="group:g1")
