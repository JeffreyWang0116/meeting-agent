"""Orchestrator：正規化 → 分析 → 存檔 → 確認信的 pipeline 串接測試。"""
import pytest

from app.agents.decision_agent import DecisionAgent
from app.orchestrator import Orchestrator
from app.stores.local_store import LocalJsonStore
from tests.test_decision import valid_json


@pytest.fixture
def orchestrator(tmp_path):
    store = LocalJsonStore(tmp_path / "db.json")
    return (
        Orchestrator(
            decision=DecisionAgent(generate=lambda prompt: valid_json()),
            store=store,
        ),
        store,
    )


def test_full_pipeline_produces_complete_result(orchestrator):
    pipeline, store = orchestrator
    result = pipeline.process_transcript("志明：我下週一前把 prompt 寫好\r\n\r\n\r\nKevin: ok")

    assert result["meeting_id"]
    assert result["analysis"]["meeting"]["title"] == "專題進度會議"
    assert "email_draft" in result["notifications"]
    assert store.get_meeting(result["meeting_id"]) is not None
    assert len(store.list_tasks(meeting_id=result["meeting_id"])) == 1


def test_pipeline_writes_no_files_besides_the_store(tmp_path, orchestrator, monkeypatch):
    # 確認信與行事曆事件以前另外寫一份到 data/output/notifications，沒有任何地方
    # 讀它，雲端又是暫時性磁碟。內容只從回應與資料庫重產
    monkeypatch.chdir(tmp_path)
    pipeline, _ = orchestrator
    pipeline.process_transcript("志明：我下週一前把 prompt 寫好")
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == ["db.json"]


def test_empty_input_raises_value_error(orchestrator):
    pipeline, _ = orchestrator
    with pytest.raises(ValueError):
        pipeline.process_transcript("   ")


def test_disabled_todos_feature_creates_no_tasks(orchestrator):
    """代辦事項功能沒開時，不只畫面不顯示，任務庫也不該真的多出任務。"""
    pipeline, store = orchestrator
    result = pipeline.process_transcript(
        "志明：我下週一前把 prompt 寫好", features=set()
    )
    assert result["analysis"]["todos"] == []
    assert result["analysis"]["meeting"]["summary"] is None
    assert store.list_tasks(meeting_id=result["meeting_id"]) == []


def test_analysis_is_json_serializable(orchestrator):
    import json

    pipeline, _ = orchestrator
    result = pipeline.process_transcript("測試會議內容")
    json.dumps(result, ensure_ascii=False)  # 不應丟例外（date 需序列化為字串）


# ---- 講者姓名 ----
# 講者一律維持「講者A/B/C」代號，只有預錄聲音比對出的姓名（使用者自己填的）會自動套用。

def test_stored_transcript_uses_prior_names(tmp_path):
    """預錄姓名套用後，存進資料庫的必須是換上姓名的版本。"""
    store = LocalJsonStore(tmp_path / "db.json")
    pipeline = Orchestrator(
        decision=DecisionAgent(generate=lambda prompt: valid_json()),
        store=store,
    )
    result = pipeline.process_transcript(
        "[0:05] 講者A：我下週一前把 prompt 寫好", speaker_prior={"講者A": "吳宗憲"}
    )
    stored = store.get_meeting(result["meeting_id"])
    assert "吳宗憲：" in stored["transcript"]


def test_speakers_stay_as_codes_without_prior(orchestrator):
    pipeline, _ = orchestrator
    result = pipeline.process_transcript("[0:05] 講者A：我是吳宗憲，我下週一前把 prompt 寫好")
    assert "講者A：" in result["transcript"]
    assert result["speaker_names"] == []


# ---- 出席者名單（預錄聲音樣本的人）----

def test_attendees_reach_the_decision_agent(tmp_path):
    """出席者名單要一路帶到分析：只停在 API 層等於沒做。"""
    captured = {}
    store = LocalJsonStore(tmp_path / "db.json")

    def fake_generate(prompt):
        captured["prompt"] = prompt
        return valid_json()

    pipeline = Orchestrator(
        decision=DecisionAgent(generate=fake_generate),
        store=store,
    )
    pipeline.process_transcript("[0:05] 講者A：大家好", attendees=["王小明"])
    assert "王小明" in captured["prompt"]


def test_transcribe_models_are_saved_with_the_meeting(orchestrator):
    pipeline, store = orchestrator
    result = pipeline.process_transcript(
        "志明：我下週一前把 prompt 寫好", transcribe_models=["gemini-3.5-flash-lite"]
    )
    saved = store.get_meeting(result["meeting_id"])
    assert saved["transcribe_models"] == ["gemini-3.5-flash-lite"]


def test_pasted_transcript_has_no_transcribe_models(orchestrator):
    pipeline, store = orchestrator
    result = pipeline.process_transcript("志明：我下週一前把 prompt 寫好")
    assert store.get_meeting(result["meeting_id"]).get("transcribe_models", []) == []
