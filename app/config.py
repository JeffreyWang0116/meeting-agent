"""環境設定：從 .env / 環境變數讀取。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def _under_base(value: str | None) -> Path | None:
    """把 .env 裡的相對路徑接到專案根目錄底下。

    .env 就放在專案根目錄，使用者寫 ./firebase-service-account.json 時指的是
    那裡。但相對路徑實際上是對「啟動時的工作目錄」解析的——從上層目錄、IDE
    或預覽工具啟動就會找不到檔案，而且錯誤看起來像是金鑰根本沒下載。
    絕對路徑原樣放行。
    """
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else BASE_DIR / path


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str | None = None
    # 多把 key 輪替：免費層配額爆掉（429）時自動換下一把
    gemini_api_keys: tuple[str, ...] = ()
    # 分析模型。與轉錄同樣預設用高額度的 lite：免費層 Flash 每日只有 20 次、
    # Lite 有 500 次，預設值選 Flash 會讓照著 README 部署的人很快撞牆。
    # 想要更強的推理再用 .env 覆蓋（如 gemini-3.5-flash），代價是額度剩 1/25
    gemini_model: str = "gemini-flash-lite-latest"
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
    # 預錄聲音辨識人：最多幾個人可以錄樣本（0＝停用整個功能）。上限讓送進比對
    # 的音訊量有界；人再多時嗓音相近的機率也上升，比對本來就不該當唯一依據
    live_enroll_max_speakers: int = 4
    # 聲紋比對用的模型。一場會議只打一次（相較轉錄每 45 秒一次），所以用強模型
    # 換準確度很划算——比對嗓音比轉錄吃力得多，lite 實測容易亂猜。
    # 空字串＝沿用 correct_model
    voice_match_model: str | None = "gemini-3.5-flash"
    # 上傳的長音檔分段轉錄的每段秒數（0＝不分段，整份送出）。
    # 實測整份送出 17 分鐘錄音時，Gemini 會整份放棄講者標註、時間戳也會漂掉
    transcribe_chunk_seconds: int = 240
    # 較強的轉錄模型：長檔整份單次轉錄、以及某段講者標註率過低時重跑那一段
    # （空字串＝不啟用）。不用 gemini-flash-latest：那是會飄到「當下最新版」的
    # 別名，實測常飄到過載的版本回 503，長檔整份一次呼叫撞上就整份失敗。改用
    # 釘死版本 gemini-3.5-flash（實測穩定且能聽音訊），不會被別名帶去踩過載。
    transcribe_fallback_model: str | None = "gemini-3.5-flash"
    # 單一檔案最多幾段可以動用備援模型。免費層實測額度：Flash Lite 每日 500 次、
    # Flash 每日只有 20 次——備援跑一次就吃掉每日 Flash 額度的 5%，遠高於重試
    # 的成本上限，所以預設 0（不啟用），把預算全花在便宜的 lite 重試，稀有的
    # Flash 額度留給長檔整份轉錄（見 transcribe_long_file_threshold_seconds，
    # 那才是它真正划算的地方）。需要極致品質再設成 1
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
    # 超過這個長度（秒）的檔案改用強模型（transcribe_fallback_model=flash）整份
    # 單次轉錄、不分段。實測 14 分鐘台語質詢：強模型一次聽完整場的語者分辨遠優
    # 於 lite 分段（分段會破壞它賴以分辨講者的全局脈絡）。代價是每場吃 1 次 flash
    # （每日僅 20 次）。預設 600（10 分鐘）：長檔重品質、短檔用便宜 lite 省額度。
    # 設 0＝停用，一律 lite 分段
    transcribe_long_file_threshold_seconds: int = 600
    data_dir: Path = field(default_factory=lambda: BASE_DIR / "data")
    # Firebase 金鑰：任一有值就用 Firestore 雲端儲存，否則用本地 JSON
    firebase_credentials_json: str | None = None  # service account JSON 字串（Render 用）
    firebase_credentials_file: str | None = None  # service account JSON 檔路徑（本機用）
    # 有設就要求所有 /api/* 請求帶 Authorization: Bearer <token>；不設 = 不驗證（本機開發預設）
    api_token: str | None = None
    # 單次上傳的大小上限（MB）。免費層雲端只有幾百 MB 的暫時性磁碟，寫爆之後
    # 連 db.json 都存不進去，整個服務跟著停擺。2 小時的單聲道會議錄音約
    # 60~120MB，500 已經很寬鬆；磁碟更小的方案就往下調
    max_upload_mb: int = 500
    # Firebase Auth（Google 登入）。填了 web 金鑰就啟用真正的帳號制：前端用
    # 這組公開設定跑登入流程，後端驗 ID token 取 uid 當使用者，資料一人一份。
    # 不填＝維持單人模式（見 app/auth.py）
    firebase_web_api_key: str | None = None
    firebase_auth_domain: str | None = None
    firebase_project_id: str | None = None
    # 這份服務是不是跑在公開網址上。Render 一定會設 RENDER=true，官方文件明講
    # 就是給程式判斷用的。本機不設認證是刻意的方便，公開網址不設認證是災難——
    # create_app 要靠這個旗標分辨兩者
    is_public_deploy: bool = False
    # 「我知道，就是要開一個沒有門的公開站」。預設 False：忘記設定與刻意不設定，
    # 後果差太多，不該共用同一個沉默的預設值
    allow_no_auth: bool = False

    @property
    def auth_enabled(self) -> bool:
        """前端跑得動登入流程才算啟用：少了 authDomain，Google 登入視窗
        根本開不起來，那時要求 ID token 只會把所有人擋在門外。"""
        return bool(self.firebase_web_api_key and self.firebase_auth_domain)

    @property
    def auth_configured(self) -> bool:
        """有任何一種把關方式：帳號制，或退而求其次的共用鑰匙。"""
        return self.auth_enabled or bool(self.api_token)


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
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest"),
        transcribe_engine=os.environ.get("TRANSCRIBE_ENGINE", "local").lower(),
        transcribe_model=os.environ.get("TRANSCRIBE_MODEL", "gemini-flash-lite-latest"),
        correct_model=os.environ.get("CORRECT_MODEL", "gemini-flash-lite-latest"),
        whisper_model=os.environ.get("WHISPER_MODEL") or None,
        whisper_device=os.environ.get("WHISPER_DEVICE") or None,
        live_chunk_seconds=int(os.environ.get("LIVE_CHUNK_SECONDS", "45")),
        live_enroll_max_speakers=int(
            os.environ.get("LIVE_ENROLL_MAX_SPEAKERS", "4")
        ),
        voice_match_model=(
            os.environ.get("VOICE_MATCH_MODEL", "gemini-3.5-flash") or None
        ),
        transcribe_chunk_seconds=int(os.environ.get("TRANSCRIBE_CHUNK_SECONDS", "240")),
        transcribe_fallback_model=(
            os.environ.get("TRANSCRIBE_FALLBACK_MODEL", "gemini-3.5-flash") or None
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
        transcribe_long_file_threshold_seconds=int(
            os.environ.get("TRANSCRIBE_LONG_FILE_THRESHOLD_SECONDS", "600")
        ),
        data_dir=_under_base(os.environ.get("DATA_DIR")) or BASE_DIR / "data",
        firebase_credentials_json=os.environ.get("FIREBASE_CREDENTIALS_JSON") or None,
        firebase_credentials_file=(
            str(resolved)
            if (resolved := _under_base(
                os.environ.get("FIREBASE_CREDENTIALS_FILE")
                or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            ))
            else None
        ),
        api_token=os.environ.get("API_TOKEN") or None,
        max_upload_mb=int(os.environ.get("MAX_UPLOAD_MB", "500")),
        firebase_web_api_key=os.environ.get("FIREBASE_WEB_API_KEY") or None,
        firebase_auth_domain=os.environ.get("FIREBASE_AUTH_DOMAIN") or None,
        firebase_project_id=os.environ.get("FIREBASE_PROJECT_ID") or None,
        is_public_deploy=bool(os.environ.get("RENDER")),
        allow_no_auth=os.environ.get("ALLOW_NO_AUTH", "").strip().lower()
        in {"1", "true", "yes"},
    )
