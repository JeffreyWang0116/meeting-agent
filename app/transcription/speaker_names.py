"""把逐字稿的講者代號換成姓名：驗證與本地改寫。

轉錄一律輸出「講者A/B/C」代號，系統不從對話內容推斷姓名——實測（立法院質詢）
模型會把講者自己說的「主席好」「謝謝王委員的提問」當成他本人，被批評的第三人也
被安成講者，猜錯的名字比代號更糟。姓名只來自兩個地方：

- 使用者會前預錄聲音樣本時自己填的姓名（VoiceMatcher / pyannote identify 比對）
- 使用者事後在結果頁或歷史會議點講者改名（前端 js/speakers.js）

這裡只負責前者的安全套用：模型只回報「誰是誰」，改寫由本地執行並驗證結構，
行結構、時間標記都在我們自己的控制下。
"""
from __future__ import annotations

import re

from app.transcription.segments import SPEAKER_RE, replace_speaker, speaker_of

# 行首時間標記（與 corrector_agent、前端 TIME_RE 對齊）
_TIME_MARKER = re.compile(r"\[\d{1,2}(?::\d{2}){1,2}\]")

# 姓名長度上限。真實姓名或職稱不會超過這個長度
MAX_NAME_LEN = 20


def is_safe_name(name: str) -> bool:
    """姓名可不可以安全地寫進逐字稿的講者欄。"""
    if not name or len(name) > MAX_NAME_LEN:
        return False
    # 冒號會在行首造出第二個假標籤；換行會把一行拆成兩行破壞時間軸
    if "\n" in name or "：" in name or ":" in name:
        return False
    if "[" in name or "]" in name:
        return False
    # 對應成另一個代號不是命名，是重新編號
    return not SPEAKER_RE.match(f"{name}：")


def apply_speaker_names(
    transcript: str, mapping: dict[str, str]
) -> tuple[str, list[dict]]:
    """在本地把代號換成姓名，回傳 (新逐字稿, 實際生效的對應)。

    只要有任何一筆對應不合格就整批放棄：一份「一半代號一半姓名」的逐字稿，
    比全部維持代號更難讀。
    """
    labels_present = {s for s in (speaker_of(ln) for ln in transcript.split("\n")) if s}
    safe: dict[str, str] = {}
    for label, name in mapping.items():
        if label not in labels_present or not is_safe_name(name):
            return transcript, []
        safe[label] = name
    # 兩個代號指向同一人是比對模型不該擅自做的合併判斷
    if len(set(safe.values())) != len(safe):
        return transcript, []
    if not safe:
        return transcript, []

    counts: dict[str, int] = {}
    lines = []
    for line in transcript.split("\n"):
        label = speaker_of(line)
        if label in safe:
            line = replace_speaker(line, safe[label])
            counts[label] = counts.get(label, 0) + 1
        lines.append(line)
    text = "\n".join(lines)

    # 最終保險：行數與時間標記數量都不該變（與 apply_corrections 同一道防線）
    if text.count("\n") != transcript.count("\n") or len(
        _TIME_MARKER.findall(text)
    ) != len(_TIME_MARKER.findall(transcript)):
        return transcript, []
    return text, [
        {"label": label, "name": name, "count": counts.get(label, 0)}
        for label, name in safe.items()
    ]
