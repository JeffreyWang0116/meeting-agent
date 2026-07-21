"""Orchestrator：四個 Agent 的 pipeline 串接測試。"""
import pytest

from app.agents.decision_agent import DecisionAgent
from app.agents.executor_agent import ExecutorAgent
from app.agents.notifier_agent import NotifierAgent
from app.agents.parser_agent import ParserAgent
from app.orchestrator import Orchestrator
from app.stores.local_store import LocalJsonStore
from tests.test_decision import valid_json


@pytest.fixture
def orchestrator(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    return (
        Orchestrator(
            parser=ParserAgent(),
            decision=DecisionAgent(generate=lambda prompt: valid_json()),
            executor=ExecutorAgent(store),
            notifier=NotifierAgent(tmp_path / "notifications"),
        ),
        store,
    )


def test_full_pipeline_produces_complete_result(orchestrator):
    pipeline, store = orchestrator
    result = pipeline.process_transcript("鈺翔：我下週一前把 prompt 寫好\r\n\r\n\r\nKevin: ok")

    assert result["meeting_id"]
    assert result["analysis"]["meeting"]["title"] == "專題進度會議"
    assert "email_draft" in result["notifications"]
    assert store.get_meeting(result["meeting_id"]) is not None
    assert len(store.list_tasks(meeting_id=result["meeting_id"])) == 1


def test_empty_input_raises_value_error(orchestrator):
    pipeline, _ = orchestrator
    with pytest.raises(ValueError):
        pipeline.process_transcript("   ")


def test_disabled_todos_feature_creates_no_tasks(orchestrator):
    """代辦事項功能沒開時，不只畫面不顯示，任務庫也不該真的多出任務。"""
    pipeline, store = orchestrator
    result = pipeline.process_transcript(
        "鈺翔：我下週一前把 prompt 寫好", features=set()
    )
    assert result["analysis"]["todos"] == []
    assert result["analysis"]["meeting"]["summary"] is None
    assert store.list_tasks(meeting_id=result["meeting_id"]) == []


def test_analysis_is_json_serializable(orchestrator):
    import json

    pipeline, _ = orchestrator
    result = pipeline.process_transcript("測試會議內容")
    json.dumps(result, ensure_ascii=False)  # 不應丟例外（date 需序列化為字串）


# ---- 講者代號換成姓名 ----
# 轉錄一律輸出「講者A/B/C」，姓名在 pipeline 最後一步統一填回。
# 順序在校正之後：先修掉同音錯字，姓名判讀才不會被誤植的稱謂誤導。

def _namer(mapping):
    import json

    from app.agents.speaker_namer_agent import SpeakerNamerAgent

    reply = json.dumps(
        {"speakers": [{"label": k, "name": v} for k, v in mapping.items()]},
        ensure_ascii=False,
    )
    return SpeakerNamerAgent(api_key="k", generate=lambda p: reply)


def test_speaker_codes_are_replaced_with_names(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    pipeline = Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(generate=lambda prompt: valid_json()),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(tmp_path / "notifications"),
        namer=_namer({"講者A": "吳宗憲"}),
    )
    result = pipeline.process_transcript("[0:05] 講者A：我下週一前把 prompt 寫好")
    assert "吳宗憲：" in result["transcript"]
    assert result["speaker_names"][0]["name"] == "吳宗憲"


def test_stored_transcript_uses_the_named_version(tmp_path):
    """存進資料庫的必須是換上姓名的版本，否則畫面與匯出會看到代號。"""
    store = LocalJsonStore(tmp_path / "db.json")
    pipeline = Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(generate=lambda prompt: valid_json()),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(tmp_path / "notifications"),
        namer=_namer({"講者A": "吳宗憲"}),
    )
    result = pipeline.process_transcript("[0:05] 講者A：我下週一前把 prompt 寫好")
    stored = store.get_meeting(result["meeting_id"])
    assert "吳宗憲：" in stored["transcript"]


def test_pipeline_works_without_a_namer(orchestrator):
    """沒有注入 namer 時維持原行為，代號原樣保留。"""
    pipeline, _ = orchestrator
    result = pipeline.process_transcript("[0:05] 講者A：我下週一前把 prompt 寫好")
    assert "講者A：" in result["transcript"]
    assert result["speaker_names"] == []
