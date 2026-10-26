"""推播訂閱的儲存。

與範本、回報同一套：存在工作區底下、不進備份。多一個 all_push_subscriptions()
是因為排程要掃全站——它沒有「當下的使用者」可言。
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


def sub(sid="s1", user="uid-A", **extra):
    return {
        "id": sid, "user": user, "uid": "uid-A",
        "endpoint": f"https://fcm.googleapis.com/fcm/send/{sid}",
        "keys": {"p256dh": "pub", "auth": "secret"},
        "tz_offset": 480, "hour": 9,
        **extra,
    }


def test_saved_subscription_reads_back(store):
    store.save_push_subscription(sub())

    got = store.list_push_subscriptions("uid-A")
    assert len(got) == 1
    assert got[0]["keys"]["auth"] == "secret"


def test_resubscribing_the_same_device_overwrites(store):
    """同一支手機重新訂閱不該多長一筆出來，否則每天會收到好幾則一樣的。"""
    store.save_push_subscription(sub())
    store.save_push_subscription(sub(hour=20))

    got = store.list_push_subscriptions("uid-A")
    assert len(got) == 1
    assert got[0]["hour"] == 20


def test_subscriptions_are_per_workspace(store):
    store.save_push_subscription(sub("s1", "uid-A"))
    store.save_push_subscription(sub("s2", "group:g1"))

    assert [s["id"] for s in store.list_push_subscriptions("uid-A")] == ["s1"]
    assert [s["id"] for s in store.list_push_subscriptions("group:g1")] == ["s2"]


def test_the_scheduler_sees_every_workspace(store):
    """排程在沒有登入者的情況下跑，得拿得到全部。"""
    store.save_push_subscription(sub("s1", "uid-A"))
    store.save_push_subscription(sub("s2", "group:g1"))

    assert {s["id"] for s in store.all_push_subscriptions()} == {"s1", "s2"}


def test_deleting_one(store):
    store.save_push_subscription(sub())
    store.delete_push_subscription("s1")

    assert store.list_push_subscriptions("uid-A") == []
    assert store.all_push_subscriptions() == []


def test_deleting_one_that_is_already_gone_is_fine(store):
    """排程發現訂閱失效時會刪它，同一輪可能有兩筆指向同一支手機。"""
    store.delete_push_subscription("nope")


def test_subscriptions_are_not_part_of_the_backup(store):
    """還原備份不該把手機的訂閱清掉——那會讓通知默默停掉而且沒人發現。"""
    store.save_push_subscription(sub())

    assert "push_subscriptions" not in store.export_all(user="uid-A")
    store.import_all({"meetings": [], "tasks": []}, user="uid-A")
    assert len(store.list_push_subscriptions("uid-A")) == 1
