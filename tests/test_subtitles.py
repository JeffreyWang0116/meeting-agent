"""逐字稿轉字幕（.srt / .vtt）。

有了字幕檔，會議錄影就能在播放器裡邊看邊讀，也能丟進剪輯軟體。逐字稿裡
本來就有時間戳與講者，缺的只是「這句講到什麼時候」——那要靠下一句的開始
時間推算。
"""
from __future__ import annotations

from app.subtitles import TAIL_SECONDS, to_srt, to_vtt

TRANSCRIPT = """[0:01] 講者A：今天要談預算
[0:08] 講者B：我覺得維持原案就好
[1:05] 講者A：那就這樣決定"""


def test_each_line_becomes_a_cue():
    out = to_srt(TRANSCRIPT)

    assert out.count("-->") == 3
    assert "1\n00:00:01,000 --> 00:00:08,000" in out


def test_a_cue_ends_when_the_next_one_starts():
    """沒有結束時間的字幕檔播放器會整句卡在畫面上，或乾脆不顯示。"""
    out = to_srt(TRANSCRIPT)

    assert "00:00:08,000 --> 00:01:05,000" in out


def test_the_last_cue_gets_a_fixed_tail():
    out = to_srt(TRANSCRIPT)

    assert f"00:01:05,000 --> 00:01:{5 + TAIL_SECONDS:02d},000" in out


def test_the_speaker_is_kept_in_the_text():
    """字幕上看得到誰在講，不然多人會議的字幕沒辦法讀。"""
    assert "講者A：今天要談預算" in to_srt(TRANSCRIPT)


def test_vtt_has_the_header_and_dots_instead_of_commas():
    out = to_vtt(TRANSCRIPT)

    assert out.startswith("WEBVTT\n")
    assert "00:00:01.000 --> 00:00:08.000" in out
    assert "," not in out.split("-->")[0].split("\n")[-1]


def test_hours_are_handled():
    out = to_srt("[1:02:03] 講者A：很長的會議")

    assert "01:02:03,000 -->" in out


def test_lines_without_a_timestamp_join_the_previous_cue():
    """換行的長句、以及校正後多出來的行，不該各自變成沒有時間的字幕。"""
    out = to_srt("[0:01] 講者A：第一句\n接著講下去\n[0:10] 講者B：換人")

    assert out.count("-->") == 2
    assert "第一句 接著講下去" in out


def test_a_transcript_with_no_timestamps_produces_nothing():
    """沒有時間就做不出字幕。硬做出一個全部擠在 0 秒的檔案更糟。"""
    assert to_srt("講者A：這份逐字稿沒有時間戳") == ""
    assert to_vtt("") == ""


def test_two_lines_at_the_same_second_do_not_make_a_zero_length_cue():
    out = to_srt("[0:05] 講者A：很快\n[0:05] 講者B：也很快")

    assert "00:00:05,000 --> 00:00:05,000" not in out
