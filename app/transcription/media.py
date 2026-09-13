"""ffmpeg 媒體處理：從影片檔抽出聲音軌。

只取聲音軌（設計決策）：畫面內容不納入分析。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".avi", ".wmv", ".flv",
    ".m4v", ".mpg", ".mpeg", ".ts", ".webm",
}

# winget 裝完 ffmpeg 後，已開啟的終端機 PATH 不會更新；直接找 winget 的捷徑位置當後備
_WINGET_FFMPEG = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"
)


# 講者分離上傳用的編碼：PoC 實測 Opus 32kbps 與 16k FLAC 分群結果 99.6% 一致、大小 1/6
_OPUS_ARGS = ("-c:a", "libopus", "-b:a", "32k", "-application", "voip")


class MediaError(Exception):
    pass


def _ffmpeg_cmd() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    if _WINGET_FFMPEG.is_file():
        return str(_WINGET_FFMPEG)
    return None


def _ffprobe_cmd() -> str | None:
    found = shutil.which("ffprobe")
    if found:
        return found
    candidate = _WINGET_FFMPEG.with_name("ffprobe.exe")
    return str(candidate) if candidate.is_file() else None


def ffmpeg_available() -> bool:
    return _ffmpeg_cmd() is not None


def is_video(path: str | Path) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTS


def extract_audio(input_path: str | Path, output_path: str | Path | None = None) -> Path:
    """抽出單聲道 16kHz WAV（whisper 的標準輸入格式）。"""
    input_path = Path(input_path)
    output_path = (
        Path(output_path)
        if output_path
        else input_path.parent / f"{input_path.stem}_audio.wav"
    )
    cmd = [
        _ffmpeg_cmd() or "ffmpeg", "-y",
        "-i", str(input_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(output_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg 抽取音軌失敗：{(proc.stderr or '').strip()[-500:]}")
    return output_path


def encode_for_diarization(
    input_path: str | Path,
    output_path: str | Path | None = None,
    max_seconds: float | None = None,
) -> Path:
    """壓成單聲道 Opus 32kbps，給 pyannote 講者分離上傳用。

    PoC（77 分鐘協商）：16k FLAC 108MB 上傳 189 秒，Opus 18MB 只要 24 秒，
    分群結果逐 0.1 秒比對 99.6% 一致。講者分離只需要嗓音特徵，不需要無損音質；
    上傳才是整段流程裡最慢的一步。

    max_seconds：只取開頭這麼多秒（voiceprint 樣本上限 30 秒）。
    """
    input_path = Path(input_path)
    output_path = (
        Path(output_path)
        if output_path
        else input_path.parent / f"{input_path.stem}_diarize.ogg"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_ffmpeg_cmd() or "ffmpeg", "-y"]
    if max_seconds:
        cmd += ["-t", f"{max_seconds:g}"]
    cmd += [
        "-i", str(input_path),
        "-vn", "-ac", "1", "-ar", "16000", *_OPUS_ARGS,
        str(output_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg 壓縮音檔失敗：{(proc.stderr or '').strip()[-500:]}")
    return output_path


def concat_for_diarization(
    pieces: list[tuple[str | Path, float]], output_path: str | Path
) -> list[float]:
    """即時聆聽：把各段錄音串成一份 Opus 給講者分離，回傳每段實際用了幾秒。

    pieces：[(錄音段, 開頭要略過的秒數)]。前端讓相鄰兩段重疊幾秒，那幾秒前一段
    已經有了，不略過的話同一段聲音會出現兩次、講者時間軸整條錯位。

    長度量的是轉出來的 wav 而不是原檔：MediaRecorder 錄的 webm 常沒有長度資訊。
    呼叫端靠這些長度把分群結果的時間換回整場時間，所以任何一段量不到就放棄。
    """
    output_path = Path(output_path)
    work_dir = output_path.parent
    work_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = _ffmpeg_cmd() or "ffmpeg"
    wavs: list[Path] = []
    list_file = work_dir / f"{output_path.stem}_concat.txt"
    try:
        durations: list[float] = []
        for index, (source, skip) in enumerate(pieces):
            wav = work_dir / f"piece_{index:03d}.wav"
            wavs.append(wav)
            cmd = [ffmpeg, "-y"]
            if skip:
                cmd += ["-ss", f"{skip:.3f}"]
            cmd += ["-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", str(wav)]
            proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
            if proc.returncode != 0:
                raise MediaError(f"ffmpeg 轉換錄音段失敗：{(proc.stderr or '').strip()[-500:]}")
            length = audio_duration(wav)
            if length is None:
                raise MediaError(f"取不到錄音段長度：{Path(source).name}")
            durations.append(length)
        # concat demuxer 的清單檔：路徑用單引號包起來，路徑裡的單引號要寫成 '\''
        list_file.write_text(
            "".join(
                "file '" + str(w.resolve()).replace("'", "'\\''") + "'\n" for w in wavs
            ),
            encoding="utf-8",
        )
        cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
               *_OPUS_ARGS, str(output_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if proc.returncode != 0:
            raise MediaError(f"ffmpeg 串接錄音段失敗：{(proc.stderr or '').strip()[-500:]}")
        return durations
    finally:
        for temp in (*wavs, list_file):
            temp.unlink(missing_ok=True)


def audio_duration(path: str | Path) -> float | None:
    """音檔長度（秒）。取不到就回 None——呼叫端據此決定要不要分段。"""
    probe = _ffprobe_cmd()
    if not probe:
        return None
    cmd = [
        probe, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        return None
    try:
        return float((proc.stdout or "").strip())
    except ValueError:
        return None


def cut_clip(
    input_path: str | Path,
    start_seconds: float,
    end_seconds: float,
    output_path: str | Path | None = None,
) -> Path:
    """剪出 [start_seconds, end_seconds) 這段音訊（供聲紋接力當講者的聲音樣本）。

    輸出統一單聲道 16kHz WAV，與 extract_audio／split_audio 一致——不需要
    額外轉檔就能直接當 Gemini 的參考音訊上傳。
    """
    input_path = Path(input_path)
    output_path = (
        Path(output_path)
        if output_path
        else input_path.parent
        / f"{input_path.stem}_clip_{int(start_seconds)}_{int(end_seconds)}.wav"
    )
    cmd = [
        _ffmpeg_cmd() or "ffmpeg", "-y",
        "-ss", f"{start_seconds:.3f}", "-t", f"{end_seconds - start_seconds:.3f}",
        "-i", str(input_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(output_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg 剪音檔失敗：{(proc.stderr or '').strip()[-500:]}")
    return output_path


def split_audio(
    input_path: str | Path,
    chunk_seconds: int,
    output_dir: str | Path | None = None,
    overlap_seconds: int = 0,
) -> list[Path]:
    """把音檔切成每段 chunk_seconds 秒，回傳依序排好的片段路徑。

    overlap_seconds > 0 時，第二段起會往前多抓這麼多秒——讓轉錄模型聽得到
    前一段的結尾，才有辦法把同一個嗓音對應回既有的講者標籤（只給名單沒用，
    模型沒聽過前一段）。重疊的內容由呼叫端依時間戳濾掉。

    輸出統一是單聲道 16kHz WAV，與 extract_audio 一致。
    """
    input_path = Path(input_path)
    out_dir = Path(output_dir) if output_dir else input_path.parent / f"{input_path.stem}_chunks"
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = audio_duration(input_path)
    if duration is None:
        raise MediaError("取不到音檔長度，無法分段")

    ffmpeg = _ffmpeg_cmd() or "ffmpeg"
    paths: list[Path] = []
    index, own_start = 0, 0.0
    while own_start < duration:
        # 第一段不需要重疊（前面沒有東西可對照）
        seek = own_start if index == 0 else max(0.0, own_start - overlap_seconds)
        length = own_start + chunk_seconds - seek
        dest = out_dir / f"chunk_{index:03d}.wav"
        cmd = [
            ffmpeg, "-y",
            "-ss", f"{seek:.3f}", "-t", f"{length:.3f}",
            "-i", str(input_path),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            str(dest),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if proc.returncode != 0:
            raise MediaError(f"ffmpeg 分段失敗：{(proc.stderr or '').strip()[-500:]}")
        paths.append(dest)
        own_start += chunk_seconds
        index += 1
    return paths
