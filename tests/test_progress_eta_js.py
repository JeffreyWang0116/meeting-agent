"""轉錄進度的剩餘時間推算（app/static/js/progress.js）。

上傳一小時的影片要等好幾分鐘，進度條只有百分比時完全不知道還要多久，
長段落轉錄又常常卡在同一個數字不動，看起來像當掉了。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "progress.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="需要 node 執行前端模組")


def eta(samples: list[list[float]]) -> object:
    """samples：[[經過秒數, 進度 0~1], ...]，依序餵進去，回傳最後一次的估算。"""
    script = f"""
import {{ createEta }} from {json.dumps(MODULE.as_uri())};
const e = createEta();
let out = null;
for (const [t, p] of {json.dumps(samples)}) out = e.update(t, p);
process.stdout.write(JSON.stringify(out));
"""
    proc = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_no_estimate_until_there_is_something_to_extrapolate_from():
    """開頭就報一個數字只會是亂猜，而且第一個數字錯得最離譜、印象最深。"""
    assert eta([[0, 0]]) is None
    assert eta([[2, 0.01]]) is None


def test_estimates_remaining_from_the_rate_so_far():
    # 20 秒走到 50% → 大約還要 20 秒
    assert eta([[0, 0], [10, 0.25], [20, 0.5]]) == pytest.approx(20, abs=3)


def test_slow_start_does_not_make_the_estimate_jump_around():
    """第一段最慢（模型要暖機），用整段平均速率會一路高估。"""
    steady = eta([[0, 0], [30, 0.1], [40, 0.3], [50, 0.5], [60, 0.7]])

    assert steady == pytest.approx(20, abs=10)


def test_finished_work_has_no_remaining_time():
    assert eta([[0, 0], [10, 0.5], [20, 1.0]]) == 0


def test_progress_that_goes_backwards_is_ignored():
    """進度回頭多半是重試造成的，不該讓估算變成負數。"""
    assert eta([[0, 0], [10, 0.5], [20, 0.4]]) >= 0


def test_a_stalled_progress_bar_keeps_the_last_estimate_rather_than_dropping_it():
    """長段落轉錄時進度會停住好一陣子；這時把估算清掉只會讓人更焦慮。"""
    assert eta([[0, 0], [10, 0.5], [60, 0.5]]) is not None


def fmt(seconds) -> str:
    script = f"""
import {{ formatEta }} from {json.dumps(MODULE.as_uri())};
process.stdout.write(JSON.stringify(formatEta({json.dumps(seconds)})));
"""
    proc = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_no_estimate_shows_nothing_rather_than_zero():
    assert fmt(None) == ""


@pytest.mark.parametrize("seconds,expected", [
    (0, "快好了"),
    (8, "快好了"),
    (22, "約還要 20 秒"),
    (58, "約還要 60 秒"),
    (200, "約還要 3 分鐘"),
])
def test_estimates_are_rounded_because_they_are_estimates(seconds, expected):
    assert fmt(seconds) == expected
