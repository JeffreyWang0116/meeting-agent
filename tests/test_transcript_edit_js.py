"""逐字稿就地編輯的純函式（app/static/js/transcriptedit.js）。

畫面上的泡泡是「多行合併」出來的（同講者、沒有時間標記的連續行會併成一句），
所以行號對不回原文——編輯必須先知道每個泡泡由原文哪幾行組成，改完只換掉那幾行，
而不是把整份逐字稿重新序列化。後者一個 bug 就會賠掉整場會議。

講者標籤還有「繼承」語意：轉錄只在換人時標註，同一人的後續行不重複標。所以改掉
某一句的講者時，沿用它的下一句必須補上明確標籤，否則會被一起改掉。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "transcriptedit.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="需要 node 執行前端模組")


def call(fn: str, *args) -> object:
    script = f"""
import * as m from {json.dumps(MODULE.as_uri())};
const out = m[{json.dumps(fn)}](...{json.dumps(args, ensure_ascii=False)});
process.stdout.write(JSON.stringify(out));
"""
    proc = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


SAMPLE = "\n".join([
    "[0:01] 講者A：大家好",
    "今天要談預算",
    "[0:20] 講者B：我同意",
])


def test_bubbles_know_which_source_lines_they_came_from():
    bubbles = call("editableBubbles", SAMPLE, [])

    assert [b["text"] for b in bubbles] == ["大家好 今天要談預算", "我同意"]
    assert [[b["from"], b["to"]] for b in bubbles] == [[0, 1], [2, 2]]
    assert [b["speaker"] for b in bubbles] == ["講者A", "講者B"]
    assert [b["time"] for b in bubbles] == ["0:01", "0:20"]


def test_editing_text_replaces_only_that_bubbles_lines():
    out = call("editBubble", SAMPLE, 0, {"text": "大家好，今天談預算"})

    assert out == "\n".join(["[0:01] 講者A：大家好，今天談預算", "[0:20] 講者B：我同意"])


def test_editing_keeps_other_bubbles_untouched():
    out = call("editBubble", SAMPLE, 1, {"text": "我反對"})

    assert out.splitlines()[:2] == ["[0:01] 講者A：大家好", "今天要談預算"]
    assert out.splitlines()[2] == "[0:20] 講者B：我反對"


def test_changing_a_speaker_does_not_drag_the_next_bubble_with_it():
    """中間那句沒有自己的講者標籤，是沿用上一句的。把上一句改成別人時，
    它必須補上原本的「講者A：」，否則會跟著一起被改掉。

    （沒有時間標記的續行本來就與上一句顯示成同一個泡泡，所以這裡每行都帶時間
    標記，才會是三個各自獨立的泡泡。）"""
    text = "\n".join(["[0:01] 講者A：甲", "[0:05] 乙", "[0:09] 講者B：丙"])

    out = call("editBubble", text, 0, {"speaker": "王小明"})

    assert out.splitlines() == ["[0:01] 王小明：甲", "[0:05] 講者A：乙", "[0:09] 講者B：丙"]


def test_merged_continuation_lines_are_one_bubble_and_change_together():
    """沒有時間標記的續行跟上一句本來就顯示成同一個泡泡，改講者當然一起改。"""
    text = "\n".join(["講者A：甲", "乙", "講者B：丙"])

    out = call("editBubble", text, 0, {"speaker": "王小明"})

    assert out.splitlines() == ["王小明：甲 乙", "講者B：丙"]


def test_merging_joins_a_bubble_with_the_next_one():
    out = call("mergeWithNext", SAMPLE, 0)

    assert out == "[0:01] 講者A：大家好 今天要談預算 我同意"


def test_merging_the_last_bubble_is_a_no_op():
    assert call("mergeWithNext", SAMPLE, 1) == SAMPLE


def test_splitting_cuts_a_bubble_at_the_character_offset():
    out = call("splitBubble", SAMPLE, 0, 3)

    assert out.splitlines() == ["[0:01] 講者A：大家好", "今天要談預算", "[0:20] 講者B：我同意"]


def test_splitting_at_the_edges_is_a_no_op():
    assert call("splitBubble", SAMPLE, 0, 0) == SAMPLE
    assert call("splitBubble", SAMPLE, 1, 3) == SAMPLE


def test_deleting_a_bubble_removes_its_lines():
    out = call("deleteBubble", SAMPLE, 1)

    assert out.splitlines() == ["[0:01] 講者A：大家好", "今天要談預算"]


def test_deleting_a_bubble_keeps_the_next_speakers_label():
    """被刪掉的那句是下一句講者標籤的唯一來源，刪之前要先把標籤補到下一句。"""
    text = "\n".join(["[0:01] 講者A：甲", "[0:05] 乙", "[0:09] 講者B：丙"])

    out = call("deleteBubble", text, 0)

    assert out.splitlines() == ["[0:05] 講者A：乙", "[0:09] 講者B：丙"]


def test_an_empty_edit_is_rejected_rather_than_leaving_a_blank_line():
    assert call("editBubble", SAMPLE, 0, {"text": "   "}) is None


def test_editing_never_loses_the_time_markers_of_other_lines():
    """時間標記掉了，「點會議重點跳到那一行」就跟著壞掉。"""
    text = "\n".join(["[0:01] 講者A：甲", "[0:05] 講者B：乙", "[0:09] 講者A：丙"])

    out = call("editBubble", text, 1, {"text": "乙乙乙"})

    assert [l.split("]")[0] + "]" for l in out.splitlines()] == ["[0:01]", "[0:05]", "[0:09]"]


def test_split_keeps_the_time_marker_on_the_first_half_only():
    """後半沒有自己的時間——硬掰一個會讓跳轉落在錯的位置。"""
    out = call("splitBubble", "[1:30] 講者A：前半後半", 0, 2)

    assert out.splitlines() == ["[1:30] 講者A：前半", "後半"]


def test_editing_an_index_that_does_not_exist_returns_null():
    assert call("editBubble", SAMPLE, 9, {"text": "x"}) is None


def test_clearing_a_speaker_leaves_the_text_without_a_label():
    out = call("editBubble", "講者A：甲", 0, {"speaker": ""})

    assert out == "甲"


def test_empty_transcript_has_no_bubbles():
    assert call("editableBubbles", "", []) == []
    assert call("mergeWithNext", "", 0) == ""
    assert call("deleteBubble", "", 0) == ""
