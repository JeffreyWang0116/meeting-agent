"""把「分段獨立轉錄」的結果縫合成一份逐字稿的共用工具。

即時聆聽（每 45 秒一段）與長音檔上傳（切成數分鐘一段）都會遇到同樣的問題：
每段是獨立丟給模型轉錄的，所以

1. 時間戳都是「相對本段開頭」的（第二段的 [0:05] 其實是整場的 [4:05]）
2. 講者標籤每段都重新從「講者A」編號，同一個人在不同段會變成不同代號

這裡集中處理這兩件事，讓兩條路徑共用同一套規則（也與前端 app.js 的
TIME_RE / SPEAKER_RE 對齊）。
"""
from __future__ import annotations

import re

# 行首「[1:02]」「[1:02:03]」時間標記
TIME_PREFIX_RE = re.compile(r"^\s*\[(\d{1,2}(?::\d{2}){1,2})\]\s*")

# 出現在任何位置的時間標記（計數用，驗證結構沒被改動）
TIME_MARKER_RE = re.compile(r"\[\d{1,2}(?::\d{2}){1,2}\]")

# 行首「講者A：」「Kevin:」等講者標註。務必先用 strip_time_prefix 去掉時間戳
# 再比對——時間戳裡的冒號會讓這個樣式誤抓成「[0」
SPEAKER_RE = re.compile(r"^\s*([^：:\n]{1,12})[：:]")


def format_time(seconds: float) -> str:
    """秒數 → "1:02"（未滿一小時）或 "1:00:10"（一小時以上）。"""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def parse_time_label(label: str) -> int:
    """"1:02" / "1:02:03" → 秒數。"""
    seconds = 0
    for part in label.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds


def strip_time_prefix(line: str) -> str:
    return TIME_PREFIX_RE.sub("", line)


def speaker_of(line: str) -> str | None:
    """取出一行的講者標籤（沒有就回 None）。會自動略過行首時間戳。"""
    m = SPEAKER_RE.match(strip_time_prefix(line))
    return m.group(1).strip() if m else None


def collect_speakers(text: str, known: list[str]) -> None:
    """把 text 裡新出現的講者依出場序追加進 known（就地修改）。"""
    for line in text.split("\n"):
        name = speaker_of(line)
        if name and name not in known:
            known.append(name)


def speaker_hint(speakers: list[str]) -> str | None:
    """給下一段轉錄的提示：沿用已出現過的講者標籤，別重新編號。"""
    if not speakers:
        return None
    names = "、".join(speakers)
    return (
        f"這是同一場錄音的後續片段。先前已出現的講者：{names}。"
        "請沿用相同標籤指稱同一個人的聲音，只有出現全新的聲音時才用下一個新標籤"
        "（例如已用到講者B，新的人就用講者C）。"
    )


def shift_timestamps(text: str, offset_seconds: float | None) -> str:
    """把段內的相對時間戳平移成整場時間。

    offset_seconds 是本段在整場錄音中的開始秒數。傳 None 代表呼叫端不知道
    偏移量（例如舊版前端沒帶），這時寧可把相對時間剝掉，也不要顯示錯的時間。
    模型完全沒標時間時，在段首補一個 offset 標記，至少維持段落級的時間軸。
    """
    lines = []
    any_marker = False
    for line in text.split("\n"):
        m = TIME_PREFIX_RE.match(line)
        if not m:
            lines.append(line)
            continue
        any_marker = True
        rest = line[m.end():]
        if offset_seconds is None:
            lines.append(rest)
        else:
            t = format_time(parse_time_label(m.group(1)) + offset_seconds)
            lines.append(f"[{t}] {rest}")
    if not any_marker and offset_seconds is not None and lines:
        lines[0] = f"[{format_time(offset_seconds)}] {lines[0]}"
    return "\n".join(lines)
