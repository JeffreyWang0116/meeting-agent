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

# 行首時間標記。模型不會乖乖照 [分:秒] 兩位數格式輸出，實測 gemini-flash-lite
# 會吐出「[00]」（漏掉「0:」）和「[0:1]」（秒數只有一位），所以各段一律放寬成
# 1~2 位、冒號段數 0~2。
#
# 認不得的代價是連鎖的：該行被當成「沒有時間標記」，分段平移時又補一個標記
# 變成「[4:00] [0:1] 內容」；講者擷取也會被時間戳裡的冒號截斷，把「[0」當成
# 講者名存進跨段清單，污染後續段的提示。
# 輸出一律經過 format_time 正規化成 [分:秒]，所以下游只會看到標準格式。
TIME_PREFIX_RE = re.compile(r"^\s*\[(\d{1,2}(?::\d{1,2}){0,2})\]\s*")

# 出現在任何位置的時間標記（計數用，驗證結構沒被改動）
TIME_MARKER_RE = re.compile(r"\[\d{1,2}(?::\d{1,2}){1,2}\]")

# 行首的講者標註，例如「講者A：」。務必先用 strip_time_prefix 去掉時間戳再比對
# ——時間戳裡的冒號會讓行首比對誤抓成「[0」。
#
# 只認代號、不認名字，是刻意的：模型沒有跨段記憶，允許它「聽得出名字就用名字」
# 的話，同一個人在聽得到稱謂的段落標「王委員」、聽不到的段落標「講者A」，逐字稿
# 就會出現同一人兩種標籤。轉錄階段統一輸出代號，真實姓名交由事後對應處理。
#
# 這也修掉一個更隱蔽的問題：舊樣式是「開頭 12 字內有冒號就算講者」，而中文逐字稿
# 裡「我想請問部長：…」「重點：…」俯拾皆是。它們會把 speaker_label_ratio 灌水到
# 遠高於重試門檻（模型整段沒標也判定成功），並把句子碎片存進跨段講者清單汙染提示。
SPEAKER_RE = re.compile(
    r"^\s*(?P<prefix>講者|說話者|發言人|speaker)\s*(?P<code>[A-Za-z]|\d{1,2})\s*[：:]",
    re.IGNORECASE,
)


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
    """取出一行的講者代號（沒有就回 None）。會自動略過行首時間戳。

    回傳值一律正規化成無空白、代號大寫的形式，因此「講者 a」與「講者A」會被
    當成同一個人，不會在跨段講者清單裡佔兩格。
    """
    m = SPEAKER_RE.match(strip_time_prefix(line))
    if not m:
        return None
    prefix = m.group("prefix")
    # 中文前綴沒有大小寫問題；英文的 speaker/SPEAKER 統一成 Speaker，免得同一個人
    # 因為模型每段大小寫不同而在跨段清單裡佔好幾格
    if prefix.isascii():
        prefix = prefix.capitalize()
    return f"{prefix}{m.group('code').upper()}"


def _has_speech(line: str) -> bool:
    """這一行扣掉時間戳與講者標籤後，還有沒有實際說話內容。

    分辨三種退化行與真正的內容：
    - 「[10:09] 講者A：」        → 空標籤，模型放棄轉錄
    - 「[13:10] 。」「[13:07] .」 → 只剩標點的殘骸
    - 「[8:35] 講者A：你會不會？」→ 有字，是真的內容
    判準是「有沒有任何文字或數字字元」：中文字、英數都算數，純標點與空白不算。
    """
    rest = strip_time_prefix(line)
    m = SPEAKER_RE.match(rest)
    if m:
        rest = rest[m.end():]
    return any(ch.isalnum() for ch in rest)


def drop_empty_lines(text: str) -> str:
    """丟掉沒有實際說話內容的行——只有時間戳、空的「講者A：」標籤，或只剩標點。

    模型偶爾會整段放棄轉錄內容，只吐時間戳與空標籤或一個句號（實測台語質詢
    有一整個 chunk 這樣）。這種行沒資訊，還會灌爆畫面。標註率的修正已讓這種
    chunk 觸發重試；這裡再把重試後仍殘留的空行從最終逐字稿移除。
    沒有時間戳也沒有標籤但有文字的行（純貼上的逐字稿）一律保留。
    """
    return "\n".join(ln for ln in text.split("\n") if _has_speech(ln))


def replace_speaker(line: str, name: str) -> str:
    """把一行的講者代號換成真實姓名（沒有標籤就原樣回傳）。

    只動行首那個標籤：內文裡提到的代號（有人說「剛剛講者A講的」）必須保留，
    否則整份取代會把說話內容一起改掉。
    """
    m_time = TIME_PREFIX_RE.match(line)
    prefix, rest = (line[: m_time.end()], line[m_time.end():]) if m_time else ("", line)
    m = SPEAKER_RE.match(rest)
    if not m:
        return line
    return f"{prefix}{name}：{rest[m.end():].lstrip()}"


def speaker_label_ratio(text: str) -> float:
    """有講者標籤「且有內容」的行數佔比（0~1）。空字串回傳 1.0（沒東西可標）。

    用來偵測模型這一輪放棄轉錄——有兩種放棄法都要抓到：
    1. 放棄標講者：行有內容卻沒有「講者A：」前綴。
    2. 放棄轉內容：只吐時間戳與空的「講者A：」，後面沒有話（實測台語質詢
       有整個 chunk 這樣）。這種行「有標籤」，若只看標籤就會被誤算成標好了，
       標註率虛高、重試永不觸發——所以要求標籤與內容兼具才算數。
    """
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return 1.0
    return sum(1 for ln in lines if speaker_of(ln) and _has_speech(ln)) / len(lines)


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


def transcript_tail(text: str, max_lines: int = 6) -> str:
    """取逐字稿結尾的幾行，當作下一段的對照樣本。"""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    return "\n".join(lines[-max_lines:])


def drop_lines_before(text: str, seconds: float) -> str:
    """丟掉時間戳早於 seconds 的行（時間戳須已平移成整場時間）。

    分段時每段會往前多抓一小段音訊當重疊，好讓模型聽得到前一段的聲音來對應
    講者。那段重疊會被轉錄兩次，這裡依絕對時間濾掉——比模糊比對文字可靠，
    因為兩次轉錄的用字不會完全一樣，但時間軸是同一條。
    沒有時間戳的行一律保留（無從判斷，寧可留著）。
    """
    kept = []
    for line in text.split("\n"):
        m = TIME_PREFIX_RE.match(line)
        if m and parse_time_label(m.group(1)) < seconds:
            continue
        kept.append(line)
    return "\n".join(kept)


def chunk_hint(speakers: list[str], previous_tail: str = "") -> str:
    """長音檔分段轉錄時，每一段都要帶的提示。

    「即使只有一位講者也要標註」在主 prompt 已經要求過，這裡再講一次是因為
    分段模式下的代價特別高：開場那段常是主席單人宣讀，模型一旦省略標註，
    第一段就沒有任何標籤，後續段也拿不到可沿用的講者清單，跨段一致性整條失效。

    previous_tail：前一段結尾的逐字稿（本段開頭的重疊音訊就是這些內容）。
    只給講者名單沒有用——模型沒聽過前一段，無從知道「講者C」是哪個嗓音，
    只能從自己這段重新編號，同一個人就會換標籤。附上重疊處的對照樣本，
    模型才有辦法把聲音對回既有標籤。
    """
    text = (
        "這是一段較長錄音切出來的片段，前後還有其他片段。"
        "即使本片段從頭到尾只有一位講者，也務必在每一句開頭標註講者，不可省略。"
    )
    if previous_tail:
        text += (
            "本片段的開頭與前一段重疊，那段重疊的內容在前一段被轉錄成："
            f"\n{previous_tail}\n"
            "請比對聲音，把同一個人對應回上面用過的講者標籤，不要重新編號。"
        )
    known = speaker_hint(speakers)
    return text + known if known else text


def normalize_timestamps(text: str) -> str:
    """把行首時間標記統一成 [分:秒] 格式，不改動時間值。

    整份轉錄（不分段）不會經過 shift_timestamps，模型吐出的「[00]」就會原封
    不動送到前端，而前端只認得含冒號的格式 → 那行看起來就沒有時間。
    """
    lines = []
    for line in text.split("\n"):
        m = TIME_PREFIX_RE.match(line)
        if m:
            line = f"[{format_time(parse_time_label(m.group(1)))}] {line[m.end():]}"
        lines.append(line)
    return "\n".join(lines)


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
