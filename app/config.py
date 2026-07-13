"""環境設定：從 .env / 環境變數讀取。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str | None = None
    # 別名會自動跟隨最新版 flash；.env 可改成固定版本（如 gemini-3.5-flash）求穩定
    gemini_model: str = "gemini-flash-latest"
    # 轉錄後端：local = 本地 faster-whisper（需 GPU）；gemini = 雲端用 Gemini 聽音訊
    transcribe_engine: str = "local"
    # None 代表自動：有 CUDA 用 GPU（依 VRAM 選 medium），否則 CPU + small
    whisper_model: str | None = None
    whisper_device: str | None = None
    live_chunk_seconds: int = 45
    data_dir: Path = field(default_factory=lambda: BASE_DIR / "data")


def get_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")
    return Settings(
        gemini_api_key=os.environ.get("GEMINI_API_KEY") or None,
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-flash-latest"),
        transcribe_engine=os.environ.get("TRANSCRIBE_ENGINE", "local").lower(),
        whisper_model=os.environ.get("WHISPER_MODEL") or None,
        whisper_device=os.environ.get("WHISPER_DEVICE") or None,
        live_chunk_seconds=int(os.environ.get("LIVE_CHUNK_SECONDS", "45")),
        data_dir=Path(os.environ.get("DATA_DIR", BASE_DIR / "data")),
    )
