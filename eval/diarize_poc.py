"""Phase 0 PoC：pyannoteAI 分講者 × Gemini 逐字稿的時間戳對齊實測。

不動主程式，只回答一個問題：Gemini 的 [分:秒] 時間戳夠不夠準，能不能靠它把
pyannote 的講者段落對回逐字稿的每一行。見 docs/plans/2026-09-14-pyannote-diarization.md

用法（在專案根目錄，用 .venv 的 python）：
    python -m eval.diarize_poc audio      <影片>           # 抽成 FLAC
    python -m eval.diarize_poc diarize    <audio.flac>     # 送 pyannote，存 segments JSON
    python -m eval.diarize_poc transcribe <audio.flac>     # 正式流程的 Gemini 分段轉錄
    python -m eval.diarize_poc align      <transcript.txt> <diarization.json>
輸出一律放 data/tmp/poc/（已 gitignore）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

from app.config import get_settings
from app.transcription.segments import (
    SPEAKER_RE,
    TIME_PREFIX_RE,
    format_time,
    parse_time_label,
    speaker_of,
    strip_time_prefix,
)

OUT = Path("data/tmp/poc")
API = "https://api.pyannote.ai"
TOLERANCE = 3.0
LAST_LINE_SECONDS = 15.0


def _ffmpeg() -> str:
    import static_ffmpeg.run as r

    ffmpeg, ffprobe = r.get_or_fetch_platform_executables_else_raise()
    # 讓 app.transcription.media 也找得到（它看 PATH）
    os.environ["PATH"] = str(Path(ffmpeg).parent) + os.pathsep + os.environ["PATH"]
    return ffmpeg


def cmd_audio(video: str) -> None:
    ffmpeg = _ffmpeg()
    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / "audio.flac"
    subprocess.run(
        [ffmpeg, "-y", "-i", video, "-vn", "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", str(dest)],
        check=True, capture_output=True,
    )
    print(f"{dest}  {dest.stat().st_size / 1e6:.1f} MB")


def cmd_diarize(audio: str) -> None:
    key = os.environ.get("PYANNOTE_API_KEY")
    if not key:
        sys.exit("PYANNOTE_API_KEY 沒設")
    headers = {"Authorization": f"Bearer {key}"}
    with httpx.Client(base_url=API, headers=headers, timeout=120) as client:
        media_url = f"media://poc-{uuid.uuid4().hex[:12]}{Path(audio).suffix}"
        r = client.post("/v1/media/input", json={"url": media_url})
        r.raise_for_status()
        t0 = time.time()
        with open(audio, "rb") as fh:
            put = httpx.put(
                r.json()["url"], content=fh.read(),
                headers={"Content-Type": "application/octet-stream"}, timeout=600,
            )
        put.raise_for_status()
        print(f"上傳完成 {time.time() - t0:.0f}s")

        r = client.post(
            "/v1/diarize",
            json={"url": media_url, "model": "precision-2", "exclusive": True},
        )
        r.raise_for_status()
        job = r.json()
        print("job", job)
        t0 = time.time()
        while True:
            time.sleep(10)
            r = client.get(f"/v1/jobs/{job['jobId']}")
            r.raise_for_status()
            data = r.json()
            print(f"  {time.time() - t0:4.0f}s {data['status']}")
            if data["status"] in ("succeeded", "failed", "canceled"):
                break
    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / f"diarization_{Path(audio).suffix.lstrip('.')}.json"
    dest.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    out = data.get("output") or {}
    segs = out.get("exclusiveDiarization") or out.get("diarization") or []
    print(f"{data['status']}：{len(segs)} 段、{len({s['speaker'] for s in segs})} 位講者 → {dest}")


def cmd_transcribe(audio: str) -> None:
    _ffmpeg()
    from app.transcription.gemini_transcriber import GeminiTranscriber

    s = get_settings()
    tr = GeminiTranscriber(
        api_keys=s.gemini_api_keys,
        model=s.transcribe_model,
        chunk_seconds=s.transcribe_chunk_seconds,
        fallback_model=s.transcribe_fallback_model,
        max_fallback_chunks=s.transcribe_max_fallback_chunks,
        label_retries=s.transcribe_label_retries,
        overlap_seconds=s.transcribe_overlap_seconds,
        max_retry_calls=s.transcribe_max_retry_calls,
    )
    t0 = time.time()
    text = tr.transcribe(
        audio, on_progress=lambda f, _t: print(f"  {f * 100:5.1f}%", flush=True)
    )
    dest = OUT / "transcript_gemini.txt"
    dest.write_text(text, encoding="utf-8")
    print(f"{time.time() - t0:.0f}s，{len(text.splitlines())} 行 → {dest}")


# ---- 對齊（原型；正式版會在 app/transcription/speaker_align.py 以 TDD 重寫）----

def _overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def align(transcript: str, segs: list[dict], duration: float):
    order: list[str] = []
    for s in sorted(segs, key=lambda s: s["start"]):
        if s["speaker"] not in order:
            order.append(s["speaker"])
    code = {spk: f"講者{_letters(i)}" for i, spk in enumerate(order)}

    lines = transcript.split("\n")
    stamps = [
        (i, parse_time_label(m.group(1)))
        for i, ln in enumerate(lines)
        if (m := TIME_PREFIX_RE.match(ln))
    ]
    stats = {"overlap": 0, "tolerance": 0, "unmatched": 0}
    out = list(lines)
    detail = []
    for k, (i, t) in enumerate(stamps):
        end = stamps[k + 1][1] if k + 1 < len(stamps) else min(t + LAST_LINE_SECONDS, duration)
        end = max(end, t + 1)  # 同一秒多行時至少給 1 秒
        score: dict[str, float] = {}
        for s in segs:
            ov = _overlap(t, end, s["start"], s["end"])
            if ov:
                score[s["speaker"]] = score.get(s["speaker"], 0) + ov
        how = "overlap"
        if score:
            spk = max(score, key=score.get)
        else:
            near = min(
                segs,
                key=lambda s: min(abs(s["start"] - t), abs(s["end"] - t)),
                default=None,
            )
            gap = min(abs(near["start"] - t), abs(near["end"] - t)) if near else 1e9
            if gap <= TOLERANCE:
                spk, how = near["speaker"], "tolerance"
            else:
                spk, how = None, "unmatched"
        stats[how] += 1
        rest = strip_time_prefix(lines[i])
        m = SPEAKER_RE.match(rest)
        body = rest[m.end():].lstrip() if m else rest
        label = f"{code[spk]}：" if spk else ""
        out[i] = f"[{format_time(t)}] {label}{body}"
        detail.append((t, speaker_of(lines[i]), code.get(spk), how, score))
    return "\n".join(out), stats, code, detail


def _letters(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def cmd_align(transcript_path: str, diar_path: str) -> None:
    transcript = Path(transcript_path).read_text(encoding="utf-8")
    data = json.loads(Path(diar_path).read_text(encoding="utf-8"))
    out = data["output"]
    segs = out.get("exclusiveDiarization") or out["diarization"]
    duration = max(s["end"] for s in segs)
    text, stats, code, detail = align(transcript, segs, duration)
    total = sum(stats.values())
    (OUT / "transcript_aligned.txt").write_text(text, encoding="utf-8")

    # 同一行被兩位講者瓜分的程度：第一名佔比 < 60% 視為「邊界可疑」
    shaky = sum(
        1 for *_, sc in detail if sc and max(sc.values()) / sum(sc.values()) < 0.6
    )
    print(f"講者數 pyannote={len(code)}，行數 {total}")
    for k, v in stats.items():
        print(f"  {k:9s} {v:5d}  {v / total * 100:5.1f}%")
    print(f"  邊界可疑（首位佔比<60%） {shaky}  {shaky / total * 100:.1f}%")
    gem = {g for _, g, *_ in detail if g}
    print(f"Gemini 原本標出 {len(gem)} 個代號")
    print(f"→ {OUT / 'transcript_aligned.txt'}")


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(".env")
    sys.stdout.reconfigure(encoding="utf-8")
    cmd, *args = sys.argv[1:]
    {"audio": cmd_audio, "diarize": cmd_diarize,
     "transcribe": cmd_transcribe, "align": cmd_align}[cmd](*args)


if __name__ == "__main__":
    main()
