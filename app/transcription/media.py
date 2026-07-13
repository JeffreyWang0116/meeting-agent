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
