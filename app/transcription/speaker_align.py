"""把 pyannote 的「誰在何時講話」依時間戳填回 Gemini 逐字稿的每一行。

混合式講者辨識的接合點：Gemini 分段轉錄沒有跨段記憶，同一個人跨段會換代號，
lite 模型還常把好幾位委員併成同一個代號；pyannote 對整份音檔一次分群，代號
全場一致。文字仍用 Gemini 的，講者改用 pyannote 的。

PoC（77 分鐘黨團協商）實測：Gemini 在「換人講話」那行的時間戳，距 pyannote
發言起點中位數 0.3 秒、99% 在 2 秒內——分段內的時間戳夠準，才撐得起這個做法。
"""
from __future__ import annotations

from app.transcription.segments import SPEAKER_RE, TIME_PREFIX_RE, parse_time_label

# 一行的時間區間是「本行時間戳 → 下一個時間戳」。最後一行沒有下一個，給固定長度：
# 取到音檔結尾的話，結尾若是另一位長篇發言，那一行就會被整段算給他
LAST_LINE_SECONDS = 15.0

# 同一秒標了好幾行（快速交鋒「好」「請」）時，區間長度會是 0、完全沒有重疊可算
MIN_LINE_SECONDS = 1.0


def code_map(segments: list[dict]) -> dict[str, str]:
    """pyannote 講者 → 講者A、B、C…，依首次開口時間排序。

    pyannote 的 SPEAKER_03 不代表第四個出場，照編號給代號會讓主席變成「講者D」。
    超過 26 位改用數字（「講者27」）：SPEAKER_RE 的代號只認單一字母或 1~2 位數，
    「講者AA」會被當成沒有標籤。
    """
    codes: dict[str, str] = {}
    for segment in sorted(segments, key=lambda s: s["start"]):
        speaker = segment["speaker"]
        if speaker not in codes:
            n = len(codes)
            codes[speaker] = f"講者{chr(65 + n)}" if n < 26 else f"講者{n + 1}"
    return codes


def relabel(
    transcript: str,
    segments: list[dict],
    duration: float | None,
    tolerance: float = 3.0,
) -> tuple[str, dict[str, int]]:
    """回傳 (重標後逐字稿, 統計)。統計鍵：matched / tolerance / unmatched / dropped。

    - 每行歸給與「本行區間」重疊秒數最多的講者：時間戳只精確到秒，行首那一瞬間
      常常還是上一位的尾音，只看起點會系統性地歸錯人
    - 時間戳超出音檔長度的行整行丟掉（連同續行）：PoC 裡這種行全是 lite 模型
      在休會靜音段重複吐出的幻覺。duration 未知時不丟——無從判斷
    - 落在靜音、tolerance 秒內也沒人開口的行，只拿掉講者標籤、保留文字：
      內容可能是真的，只是歸屬不明，寧可不標也不要標錯
    - 沒時間戳的續行原樣保留；整份沒時間戳或沒有任何語音段就整份原樣回傳
    """
    stats = {"matched": 0, "tolerance": 0, "unmatched": 0, "dropped": 0}
    lines = transcript.split("\n")
    stamps: dict[int, float] = {}
    for index, line in enumerate(lines):
        matched = TIME_PREFIX_RE.match(line)
        if matched:
            stamps[index] = parse_time_label(matched.group(1))
    if not stamps or not segments:
        return transcript, stats

    dropped = {
        i for i, t in stamps.items() if duration is not None and t > duration
    }
    stats["dropped"] = len(dropped)
    kept = [i for i in stamps if i not in dropped]
    next_stamp = {i: stamps[j] for i, j in zip(kept, kept[1:])}
    codes = code_map(segments)

    out: list[str] = []
    skipping = False  # 被丟掉那行後面的續行一起丟
    for index, line in enumerate(lines):
        if index in dropped:
            skipping = True
            continue
        if index not in stamps:
            if not skipping:
                out.append(line)
            continue
        skipping = False

        start = stamps[index]
        if index in next_stamp:
            end = next_stamp[index]
        else:
            end = start + LAST_LINE_SECONDS
            if duration is not None:
                end = min(end, duration)
        end = max(end, start + MIN_LINE_SECONDS)

        speaker, how = _speaker_for(start, end, segments, tolerance)
        stats[how] += 1
        out.append(_with_label(line, codes[speaker] if speaker else None))
    return "\n".join(out), stats


def _speaker_for(
    start: float, end: float, segments: list[dict], tolerance: float
) -> tuple[str | None, str]:
    overlap: dict[str, float] = {}
    for segment in segments:
        seconds = min(end, segment["end"]) - max(start, segment["start"])
        if seconds > 0:
            overlap[segment["speaker"]] = overlap.get(segment["speaker"], 0.0) + seconds
    if overlap:
        return max(overlap, key=overlap.get), "matched"

    # 距離量的是「行首那一點」到段落：量整個區間的話，區間本來就延伸到下一行，
    # 永遠貼著下一位講者，容差就失去意義
    def distance(segment: dict) -> float:
        if segment["start"] <= start <= segment["end"]:
            return 0.0
        return min(abs(segment["start"] - start), abs(start - segment["end"]))

    nearest = min(segments, key=distance)
    if distance(nearest) <= tolerance:
        return nearest["speaker"], "tolerance"
    return None, "unmatched"


def _with_label(line: str, code: str | None) -> str:
    """換掉（或補上、或拿掉）行首講者標籤，時間戳原樣保留。"""
    time_match = TIME_PREFIX_RE.match(line)
    prefix, rest = line[: time_match.end()], line[time_match.end():]
    speaker_match = SPEAKER_RE.match(rest)
    body = rest[speaker_match.end():].lstrip() if speaker_match else rest
    return f"{prefix}{code}：{body}" if code else f"{prefix}{body}"


def speaker_prior_from_identify(
    voiceprints: list[dict], codes: dict[str, str]
) -> dict[str, str]:
    """identify 輸出的 voiceprints → {講者代號: 姓名}，接既有的 speaker_prior 管線。

    沒對上（match 為空）與不在代號表裡的講者一律略過：下游 apply_speaker_names
    遇到逐字稿裡不存在的代號會整批放棄，連對的那幾筆一起賠掉。
    """
    prior: dict[str, str] = {}
    for item in voiceprints or []:
        code = codes.get(item.get("speaker") or "")
        name = item.get("match")
        if code and name:
            prior[code] = name
    return prior


def to_session_time(
    segments: list[dict], placements: list[tuple[float, float, float]]
) -> list[dict]:
    """即時聆聽：把「串接檔」上的段落時間換回整場會議時間。

    placements：每段錄音 (在串接檔的起點, 終點, 在整場的 offset)。前端回報的
    offset 與串接後的累計長度不一定相同（上傳失敗漏段、錄音中斷），所以不能假設
    串接檔時間就是整場時間。跨兩段邊界的語音段拆成兩半，各自平移。
    """
    result: list[dict] = []
    for segment in segments:
        for chunk_start, chunk_end, offset in placements:
            start = max(segment["start"], chunk_start)
            end = min(segment["end"], chunk_end)
            if end > start:
                result.append({
                    **segment,
                    "start": start - chunk_start + offset,
                    "end": end - chunk_start + offset,
                })
    return result


def split_by_starts(text: str, starts: list[float]) -> list[str]:
    """把整場（已重標的）逐字稿依時間切回各段錄音，回傳與 starts 等長的 list。

    starts：各段保留部分在整場的起點（遞增）。每行歸給「起點 ≤ 行時間」的最後一段；
    沒時間戳的續行跟著上一行走，第一個時間戳之前的行歸第一段。

    用途：voiceprint 停用時，預錄姓名改由 Gemini 聲紋比對，它要「一段錄音＋那段的
    逐字稿」當證據。重標前各段的逐字稿是 Gemini 的舊代號，必須換成重標後的版本，
    比對出來的名字才掛得上 pyannote 的代號。
    """
    buckets: list[list[str]] = [[] for _ in starts]
    current = 0
    for line in text.split("\n"):
        matched = TIME_PREFIX_RE.match(line)
        if matched:
            seconds = parse_time_label(matched.group(1))
            current = 0
            for index, start in enumerate(starts):
                if start <= seconds:
                    current = index
        if buckets:
            buckets[current].append(line)
    return ["\n".join(lines) for lines in buckets]
