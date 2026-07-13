"""UsageTracker：API 用量統計（口試展示 + 免費額度心安）。"""
from app.usage import UsageTracker


def test_record_and_snapshot(tmp_path):
    u = UsageTracker(tmp_path / "usage.json")
    u.record("analysis")
    u.record("analysis")
    u.record("live_chunk")

    snap = u.snapshot()
    assert snap["total"]["analysis"] == 2
    assert snap["total"]["live_chunk"] == 1
    assert snap["today"]["analysis"] == 2


def test_usage_persists_across_reload(tmp_path):
    path = tmp_path / "usage.json"
    UsageTracker(path).record("analysis")
    assert UsageTracker(path).snapshot()["total"]["analysis"] == 1


def test_empty_tracker_snapshot(tmp_path):
    snap = UsageTracker(tmp_path / "usage.json").snapshot()
    assert snap == {"today": {}, "total": {}}
