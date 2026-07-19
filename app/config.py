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
    # 多把 key 輪替：免費層配額爆掉（429）時自動換下一把
    gemini_api_keys: tuple[str, ...] = ()
    # 別名會自動跟隨最新版 flash；.env 可改成固定版本（如 gemini-3.5-flash）求穩定
    gemini_model: str = "gemini-flash-latest"
    # 轉錄後端：local = 本地 faster-whisper（需 GPU）；gemini = 雲端用 Gemini 聽音訊
    transcribe_engine: str = "local"
    # Gemini 轉錄專用模型：轉錄吃掉絕大多數請求（即時聆聽每段一次）但不需要
    # 聰明模型，預設用免費額度高的輕量版，與分析模型（gemini_model）脫鉤
    transcribe_model: str = "gemini-flash-lite-latest"
    # None 代表自動：有 CUDA 用 GPU（依 VRAM 選 medium），否則 CPU + small
    whisper_model: str | None = None
    whisper_device: str | None = None
    live_chunk_seconds: int = 45
    data_dir: Path = field(default_factory=lambda: BASE_DIR / "data")
    # Firebase 金鑰：任一有值就用 Firestore 雲端儲存，否則用本地 JSON
    firebase_credentials_json: str | None = None  # service account JSON 字串（Render 用）
    firebase_credentials_file: str | None = None  # service account JSON 檔路徑（本機用）
    # 有設就要求所有 /api/* 請求帶 Authorization: Bearer <token>；不設 = 不驗證（本機開發預設）
    api_token: str | None = None


def get_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")
    # GEMINI_API_KEYS=key1,key2,...（優先）；沒設就退回單把 GEMINI_API_KEY
    keys = tuple(
        k.strip() for k in os.environ.get("GEMINI_API_KEYS", "").split(",") if k.strip()
    )
    if not keys:
        single = os.environ.get("GEMINI_API_KEY") or None
        keys = (single,) if single else ()
    return Settings(
        gemini_api_key=keys[0] if keys else None,
        gemini_api_keys=keys,
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-flash-latest"),
        transcribe_engine=os.environ.get("TRANSCRIBE_ENGINE", "local").lower(),
        transcribe_model=os.environ.get("TRANSCRIBE_MODEL", "gemini-flash-lite-latest"),
        whisper_model=os.environ.get("WHISPER_MODEL") or None,
        whisper_device=os.environ.get("WHISPER_DEVICE") or None,
        live_chunk_seconds=int(os.environ.get("LIVE_CHUNK_SECONDS", "45")),
        data_dir=Path(os.environ.get("DATA_DIR", BASE_DIR / "data")),
        firebase_credentials_json=os.environ.get("FIREBASE_CREDENTIALS_JSON") or None,
        firebase_credentials_file=(
            os.environ.get("FIREBASE_CREDENTIALS_FILE")
            or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            or None
        ),
        api_token=os.environ.get("API_TOKEN") or None,
    )
