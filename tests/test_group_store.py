"""群組文件的儲存：本地 JSON 與 Firestore 行為一致。

「我在哪些群組、誰邀請了我」要能一次查出來；Firestore 用 member_uids／invite_emails
兩個陣列欄位做單欄位 array_contains 查詢，不必建複合索引。
"""
import pytest

from app.stores.local_store import LocalJsonStore
from tests.test_firestore_store import make_store as make_firestore


@pytest.fixture(params=["local", "firestore"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalJsonStore(tmp_path / "db.json")
    return make_firestore()


def group(gid, owner="uid-amy", members=("uid-amy",), invites=()):
    return {
        "id": gid, "name": f"群組{gid}", "owner": owner,
        "members": [{"uid": u, "email": f"{u}@x.com", "role": "owner" if u == owner else "editor"} for u in members],
        "member_uids": list(members),
        "invites": [{"email": e, "role": "viewer"} for e in invites],
        "invite_emails": list(invites),
    }


def test_save_get_and_overwrite(store):
    store.save_group(group("g1"))
    assert store.get_group("g1")["name"] == "群組g1"
    updated = dict(group("g1"), name="新名字")
    store.save_group(updated)
    assert store.get_group("g1")["name"] == "新名字"
    assert store.get_group("nope") is None


def test_groups_for_finds_memberships_and_invites(store):
    store.save_group(group("g1", members=("uid-amy", "uid-bob")))
    store.save_group(group("g2", invites=("bob@gmail.com",)))
    store.save_group(group("g3"))
    found = {g["id"] for g in store.groups_for("uid-bob", "bob@gmail.com")}
    assert found == {"g1", "g2"}
    assert {g["id"] for g in store.groups_for("uid-bob", None)} == {"g1"}


def test_delete_group(store):
    store.save_group(group("g1"))
    store.delete_group("g1")
    assert store.get_group("g1") is None
    assert store.groups_for("uid-amy", None) == []
