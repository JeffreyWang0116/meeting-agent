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
    # 錯字校正專用模型：機械性工作（找同音錯字），不需要聰明模型，
    # 與分析模型脫鉤才不會在 GEMINI_MODEL 換成高階模型時一起吃掉稀有額度
    correct_model: str = "gemini-flash-lite-latest"
    # None 代表自動：有 CUDA 用 GPU（依 VRAM 選 medium），否則 CPU + small
    whisper_model: str | None = None
    whisper_device: str | None = None
    live_chunk_seconds: int = 45
    # 上傳的長音檔分段轉錄的每段秒數（0＝不分段，整份送出）。
    # 實測整份送出 17 分鐘錄音時，Gemini 會整份放棄講者標註、時間戳也會漂掉
    transcribe_chunk_seconds: int = 240
    # 某一段的講者標註率過低時，改用這個較強的模型重跑那一段（空字串＝不啟用）。
    # 只在失敗的段落動用，免費額度較低的模型才不會被整場錄音吃光
    transcribe_fallback_model: str | None = "gemini-flash-latest"
    # 單一檔案最多幾段可以動用備援模型。免費層實測額度：Flash Lite 每日 500 次、
    # Flash 每日只有 20 次——備援跑一次就吃掉每日 Flash 額度的 5%，遠高於重試
    # 的成本上限，所以預設 0（不啟用），把預算全花在便宜的 lite 重試，
    # 稀有的 Flash 額度留給分析。需要極致品質再設成 1
    transcribe_max_fallback_chunks: int = 0
    # 單一檔案總共最多幾次重試。只設每段上限的話總量會隨影片長度線性膨脹
    # （60 分鐘＝15 段 × 2 次＝30 次，佔每日 500 次額度的 6%）；設每檔上限
    # 讓重試成本與長度脫鉤，10 次＝額度的 2%
    transcribe_max_retry_calls: int = 10
    # 標註率不足時，用同一個 lite 模型重跑幾次。失敗是執行間的變異（同一輸入
    # 標註率可能 20% 也可能 100%），多試幾次的累積成功率遠比換模型划算：
    # lite 一次只佔每日額度 0.2%，Flash 一次佔 5%
    transcribe_label_retries: int = 2
    # 每段往前多抓幾秒當重疊：模型沒聽過前一段，光給講者名單無從對應嗓音，
    # 同一個人跨段就會換標籤。重疊＋提示裡的對照樣本才接得起來（0＝不重疊）
    transcribe_overlap_seconds: int = 20
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
        correct_model=os.environ.get("CORRECT_MODEL", "gemini-flash-lite-latest"),
        whisper_model=os.environ.get("WHISPER_MODEL") or None,
        whisper_device=os.environ.get("WHISPER_DEVICE") or None,
        live_chunk_seconds=int(os.environ.get("LIVE_CHUNK_SECONDS", "45")),
        transcribe_chunk_seconds=int(os.environ.get("TRANSCRIBE_CHUNK_SECONDS", "240")),
        transcribe_fallback_model=(
            os.environ.get("TRANSCRIBE_FALLBACK_MODEL", "gemini-flash-latest") or None
        ),
        transcribe_max_fallback_chunks=int(
            os.environ.get("TRANSCRIBE_MAX_FALLBACK_CHUNKS", "0")
        ),
        transcribe_max_retry_calls=int(
            os.environ.get("TRANSCRIBE_MAX_RETRY_CALLS", "10")
        ),
        transcribe_label_retries=int(os.environ.get("TRANSCRIBE_LABEL_RETRIES", "2")),
        transcribe_overlap_seconds=int(
            os.environ.get("TRANSCRIBE_OVERLAP_SECONDS", "20")
        ),
        data_dir=Path(os.environ.get("DATA_DIR", BASE_DIR / "data")),
        firebase_credentials_json=os.environ.get("FIREBASE_CREDENTIALS_JSON") or None,
        firebase_credentials_file=(
            os.environ.get("FIREBASE_CREDENTIALS_FILE")
            or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            or None
        ),
        api_token=os.environ.get("API_TOKEN") or None,
    )
