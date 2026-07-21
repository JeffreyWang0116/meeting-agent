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
