"""Executor Agent：任務分發（寫入任務庫）測試。"""
from app.agents.executor_agent import ExecutorAgent
from app.stores.local_store import LocalJsonStore
from tests.test_stores import make_analysis


def test_execute_saves_meeting_and_returns_id(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    executor = ExecutorAgent(store)

    meeting_id = executor.execute(make_analysis())

    assert store.get_meeting(meeting_id) is not None
    tasks = store.list_tasks(meeting_id=meeting_id)
    assert len(tasks) == 1
    assert tasks[0]["task"] == "完成 Prompt 初版"
