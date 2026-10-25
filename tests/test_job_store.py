"""轉錄工作紀錄的儲存：本地 JSON 與 Firestore 行為一致。

工作狀態原本只在記憶體，伺服器重新部署後正在轉的檔案整個消失，前端輪詢到 404
也不知道發生什麼事。落地之後重啟還查得到，至少能告訴使用者「中斷了」並交出
已經轉好的逐字稿。
"""
import pytest

from app.stores.local_store import LocalJsonStore
from tests.test_firestore_store import make_store as make_firestore


@pytest.fixture(params=["local", "firestore"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalJsonStore(tmp_path / "db.json")
    return make_firestore()


def job(jid, user="uid-A", status="transcribing", created_at=1000.0):
    return {"id": jid, "user": user, "status": status, "progress": 0.5,
            "transcript": "前半段", "result": None, "error": None, "created_at": created_at}


def test_missing_job_is_none(store):
    assert store.get_job("nope") is None


def test_save_get_and_overwrite(store):
    store.save_job(job("j1"))
    store.save_job({**job("j1"), "status": "done"})

    assert store.get_job("j1")["status"] == "done"


def test_list_jobs_only_returns_that_users(store):
    store.save_job(job("j1"))
    store.save_job(job("j2", user="uid-B"))

    assert [j["id"] for j in store.list_jobs("uid-A")] == ["j1"]


def test_delete_job(store):
    store.save_job(job("j1"))
    store.delete_job("j1")
    store.delete_job("j1")  # 刪兩次不報錯

    assert store.get_job("j1") is None


def test_jobs_are_not_part_of_the_backup(store):
    """還原備份會重建會議與任務；工作紀錄是暫時性的，不該跟著匯出或被還原清掉。"""
    store.save_job(job("j1"))

    assert "jobs" not in store.export_all(user="uid-A")
    store.import_all({"meetings": [], "tasks": []}, user="uid-A")
    assert store.get_job("j1") is not None
