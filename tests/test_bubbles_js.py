"""逐字稿切泡泡的純函式（app/static/js/bubbles.js）。

轉錄出來常常一句一行，照原樣畫會碎成一堆單行。規則是：帶時間標記的行自成一句
（時間軸不能被合併吃掉），沒有時間標記、同一個講者的連續行併成一句。

講者標籤有「繼承」語意：轉錄只在換人時標註，同一人的後續行不重複標，所以要
一路記住上一個講者是誰——漏掉這條，第二行之後的句子就全部變成「沒有講者」。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "bubbles.js"
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


def test_continuation_lines_join_the_bubble_above_them():
    bubbles = call("parseChatMessages", SAMPLE, [])

    assert [b["text"] for b in bubbles] == ["大家好 今天要談預算", "我同意"]
    assert [b["speaker"] for b in bubbles] == ["講者A", "講者B"]
    assert [b["time"] for b in bubbles] == ["0:01", "0:20"]


def test_a_time_marker_always_starts_a_new_bubble():
    """每行都有時間標記時不合併：合併掉就跳不回正確的時間點了。"""
    text = "\n".join(["[0:01] 講者A：甲", "[0:05] 乙", "[0:09] 講者B：丙"])

    assert [b["text"] for b in call("parseChatMessages", text, [])] == ["甲", "乙", "丙"]


def test_the_speaker_label_is_inherited_by_the_following_lines():
    text = "\n".join(["講者A：甲", "乙", "講者B：丙"])

    assert [b["speaker"] for b in call("parseChatMessages", text, [])] == ["講者A", "講者B"]


def test_a_new_speaker_starts_a_new_bubble_even_without_a_time_marker():
    """真實姓名要在 knownNames 裡才算講者，否則逐字稿裡任何「X：Y」都會被當成發言。"""
    text = "\n".join(["王小明：你好", "李小華：你好"])
    names = ["王小明", "李小華"]

    assert [b["text"] for b in call("parseChatMessages", text, names)] == ["你好", "你好"]
    # 不在名單裡時整行都是內文，兩行就照「同一個講者（都是 null）」併成一句
    assert len(call("parseChatMessages", text, [])) == 1


def test_blank_lines_are_dropped():
    text = "\n".join(["王小明：你好", "", "  ", "李小華：午安"])

    bubbles = call("parseChatMessages", text, ["王小明", "李小華"])
    assert [b["text"] for b in bubbles] == ["你好", "午安"]


def test_a_line_with_neither_speaker_nor_time_is_still_a_bubble():
    bubbles = call("parseChatMessages", "沒有講者也沒有時間的一行", [])

    assert [(b["speaker"], b["text"]) for b in bubbles] == [(None, "沒有講者也沒有時間的一行")]


def test_empty_transcript_has_no_bubbles():
    assert call("parseChatMessages", "", []) == []


def test_merged_bubble_reports_how_many_source_lines_it_holds():
    """譯文對齊要靠它：譯文沒有時間標記，只能照原文每句合併了幾行分組。"""
    msgs = call("parseChatMessages", "\n".join(["講者A：甲", "乙", "丙"]), [])

    assert [m["lines"] for m in msgs] == [3]
