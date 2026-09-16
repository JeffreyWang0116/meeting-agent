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


# ---- 講者改名：系統一律標代號，使用者結束後自己換成姓名 ----

def call(fn: str, *args):
    script = f"""
import {{ {fn} }} from {json.dumps(MODULE.as_uri())};
process.stdout.write(JSON.stringify({fn}(...{json.dumps(list(args), ensure_ascii=False)})));
"""
    proc = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_rename_only_touches_the_speaker_field():
    text = "[0:21] 講者A：王委員您好\n講者A：續行\n[2:08] 講者B：謝謝，剛剛講者A問的\n[2:30]講者AB：不是同一人"
    out = call("renameSpeakerInTranscript", text, "講者A", "翁曉玲")
    assert out == (
        "[0:21] 翁曉玲：王委員您好\n翁曉玲：續行\n[2:08] 講者B：謝謝，剛剛講者A問的\n[2:30]講者AB：不是同一人"
    )


def test_rename_accepts_halfwidth_colon_and_leading_spaces():
    out = call("renameSpeakerInTranscript", "  [1:02:03] Speaker 2: hi", "Speaker 2", "Kevin Lin")
    assert out == "  [1:02:03] Kevin Lin: hi"


def test_rename_treats_regex_characters_literally():
    out = call("renameSpeakerInTranscript", "A.B：x\nAxB：y", "A.B", "王")
    assert out == "王：x\nAxB：y"


def test_attendees_are_renamed_without_duplicates():
    assert call("renameInList", ["講者A", "講者B", "翁曉玲"], "講者A", "翁曉玲") == ["翁曉玲", "講者B"]


def test_speaker_name_validation_blocks_names_that_break_the_transcript():
    """冒號會在行首造出假講者、換行會拆行、方括號會被當成時間標記——與後端 is_safe_name 同一套。"""
    for bad in ["", "  ", "王委員：他說", "a:b", "兩\n行", "[1:00]", "王" * 21]:
        assert call("speakerNameProblem", bad), bad
    assert call("speakerNameProblem", " 翁曉玲 ") == ""


# ---- 摘要跟著改名：摘要是自由文字，沒有講者欄可以對，要防誤換 ----

def test_summary_code_is_replaced_but_not_a_longer_code():
    text = "講者A質疑調查案件偏少，講者B回應；講者AB未發言。Speaker 2 agreed, Speaker 20 left."
    assert call("renameInText", text, "講者A", "翁曉玲") == (
        "翁曉玲質疑調查案件偏少，講者B回應；講者AB未發言。Speaker 2 agreed, Speaker 20 left."
    )
    assert "Kevin agreed, Speaker 20 left" in call("renameInText", text, "Speaker 2", "Kevin")


def test_summary_rename_does_not_double_up_when_new_name_contains_old():
    """把「翁曉」改成「翁曉玲」時，摘要裡已經寫對的「翁曉玲」不能變成「翁曉玲玲」。"""
    assert call("renameInText", "翁曉提問，翁曉玲追問", "翁曉", "翁曉玲") == "翁曉玲提問，翁曉玲追問"


def test_summary_rename_skips_single_character_names():
    """單字名在中文裡到處都是（「王」「林」），整段取代會誤傷。"""
    assert call("renameInText", "王委員說明，王先生補充", "王", "王榮璋") == "王委員說明，王先生補充"


def test_summary_rename_handles_empty_text():
    assert call("renameInText", None, "講者A", "翁曉玲") == ""
