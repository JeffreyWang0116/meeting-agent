"""逐字稿轉字幕檔（.srt / .vtt）。

有了字幕檔，會議錄影就能在播放器裡邊看邊讀，也能丟進剪輯軟體當字幕軌。
逐字稿裡本來就有時間戳與講者，缺的只是「這句講到什麼時候」——那只能靠
下一句的開始時間推算，所以字幕的結束時間一律是下一句的開始。
"""
from __future__ import annotations

import re

from app.transcription.segments import parse_time_label

# 最後一句沒有「下一句」可以參照，給它一段固定的長度
TAIL_SECONDS = 5
# 同一秒內連著兩句時，至少讓前一句顯示這麼久（毫秒）。長度 0 的字幕播放器會略過
MIN_CUE_MS = 800

_LINE = re.compile(r"^\[(\d{1,2}(?::\d{2}){1,2})\]\s*(.*)$")


def _cues(transcript: str) -> list[tuple[int, str]]:
    """[(開始秒數, 文字), ...]。沒有時間戳的行併進前一句——換行的長句、
    以及校正後多出來的行，不該各自變成沒有時間的字幕。"""
    out: list[tuple[int, str]] = []
    for raw in (transcript or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _LINE.match(line)
        if m:
            out.append((parse_time_label(m.group(1)), m.group(2).strip()))
        elif out:
            start, text = out[-1]
            out[-1] = (start, f"{text} {line}".strip())
    return [(s, t) for s, t in out if t]


def _stamp(ms: int, sep: str) -> str:
    s, rest = divmod(ms, 1000)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{rest:03d}"


def _render(transcript: str, sep: str, header: str, numbered: bool) -> str:
    cues = _cues(transcript)
    if not cues:
        return ""  # 沒有時間就做不出字幕。硬做一個全部擠在 0 秒的檔案更糟
    blocks = []
    for i, (start, text) in enumerate(cues):
        nxt = cues[i + 1][0] * 1000 if i + 1 < len(cues) else (start + TAIL_SECONDS) * 1000
        begin = start * 1000
        end = max(nxt, begin + MIN_CUE_MS)
        line = f"{_stamp(begin, sep)} --> {_stamp(end, sep)}"
        blocks.append((f"{i + 1}\n" if numbered else "") + f"{line}\n{text}")
    return header + "\n\n".join(blocks) + "\n"


def to_srt(transcript: str) -> str:
    return _render(transcript, ",", "", numbered=True)


def to_vtt(transcript: str) -> str:
    return _render(transcript, ".", "WEBVTT\n\n", numbered=False)
