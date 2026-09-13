"""前端講者標籤判斷（app/static/js/speakers.js）的行為測試。

原本前端是「行首冒號前 1~12 個字就算講者」，「重點：……」「我想請問部長，這個：……」
都會被畫成一個講者泡泡、還出現在講者改名清單裡；後端 SPEAKER_RE 則只認代號，
兩邊判斷不一致。前端不能只認代號——姓名對應後逐字稿是「王小明：……」——所以規則是：
代號一律算；像名字的標籤出現兩次以上才算；出席名單上的名字出現一次也算。

speakers.js 不碰 DOM，直接用 node 載入執行；沒有 node 的環境跳過（GitHub 的
ubuntu runner 內建 node）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "speakers.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="需要 node 執行前端模組")


def run(lines: list[str], known: list[str] | None = None) -> dict:
    """回傳 {"labels": 認得的講者（排序）, "matches": 每行的 [講者, 內文] 或 null}。"""
    script = f"""
import {{ speakerLabels, matchSpeaker }} from {json.dumps(MODULE.as_uri())};
const lines = {json.dumps(lines, ensure_ascii=False)};
const labels = speakerLabels(lines, {json.dumps(known or [], ensure_ascii=False)});
const matches = lines.map(l => {{ const m = matchSpeaker(l, labels); return m ? [m.speaker, m.rest] : null; }});
process.stdout.write(JSON.stringify({{ labels: [...labels].sort(), matches }}));
"""
    proc = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_speaker_codes_count_even_when_they_appear_once():
    out = run(["講者A：大家好", "Speaker 2: hi", "發言人3：嗯"])
    assert out["labels"] == sorted(["講者A", "Speaker 2", "發言人3"])
    assert out["matches"][0] == ["講者A", "大家好"]


def test_heading_like_prefix_is_not_a_speaker():
    out = run(["講者A：先講結論", "重點：預算要保留", "講者B：同意"])
    assert "重點" not in out["labels"]
    assert out["matches"][1] is None  # 當成上一位講者的續行內容，而不是新講者


def test_sentence_with_punctuation_before_colon_is_not_a_speaker():
    out = run(["我想請問部長，這個：怎麼處理", "我想請問部長，這個：怎麼處理"])
    assert out["labels"] == []


def test_named_speaker_repeated_is_recognised():
    out = run(["王小明：開始吧", "李大華：好", "王小明：第一點"])
    assert "王小明" in out["labels"]
    assert out["matches"][2] == ["王小明", "第一點"]


def test_named_speaker_once_needs_the_attendee_list():
    lines = ["王小明：開始吧", "王小明：第一點", "許宇甄：謝謝主席"]
    assert "許宇甄" not in run(lines)["labels"]
    assert "許宇甄" in run(lines, known=["許宇甄"])["labels"]


def test_known_name_absent_from_transcript_is_not_invented():
    assert run(["講者A：嗨"], known=["王小明"])["labels"] == ["講者A"]


def test_ascii_name_with_space_is_allowed_but_not_cjk_phrases_with_spaces():
    out = run(["Kevin Lin: hello", "Kevin Lin: again", "今天 重點：一", "今天 重點：二"])
    assert out["labels"] == ["Kevin Lin"]
