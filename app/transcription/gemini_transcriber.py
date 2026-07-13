"""用 Gemini 直接聽音訊產生逐字稿（雲端無 GPU 時的轉錄後端）。

介面與本地 faster-whisper 的 Transcriber 完全一致（transcribe / device /
model_size），因此可透過 create_app 的依賴注入直接互換：本地開發用 GPU
Whisper，雲端部署設 TRANSCRIBE_ENGINE=gemini 就換成這個。

流程：把任何輸入（影片、m4a、webm 錄音段）先正規化成 Gemini 支援的音訊格式
（必要時用 ffmpeg 轉 wav），上傳 Files API，再請模型逐字轉錄。
"""
from __future__ import annotations

import time
from pathlib import Path

from app.gemini_keys import KeyPool, call_with_rotation
from app.transcription import media

# Gemini 原生支援的音訊副檔名，這些不需要再轉檔
_SAFE_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".aiff"}

_TRANSCRIBE_PROMPT = (
    "請將這段會議錄音完整逐字轉錄成繁體中文。"
    "中英夾雜的地方保留原文（英文單字、技術名詞不要翻譯）。"
    "若有多位講者，每句開頭標註講者：聽得出名字就用名字（如「小明：」），"
    "聽不出名字就用「講者A：」「講者B：」區分；只有一位講者則不必標註。"
    "只輸出逐字稿本身，不要加任何標題、說明或時間戳。"
)


class GeminiTranscribeError(Exception):
    pass


class GeminiTranscriber:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-flash-latest",
        upload=None,
        generate=None,
        api_keys=None,
    ):
        # 多把 key 輪替（429 換下一把）；單把 api_key 為向後相容寫法
        self._pool = KeyPool(api_keys if api_keys else [api_key])
        self.api_key = self._pool.current
        self.model = model
        # 供 /api/health 顯示；命名對齊 Whisper Transcriber 以免前端分歧
        self.device = "gemini"
        self.model_size = model
        self._upload = upload
        self._generate = generate

    # ---- 對外介面（與 Whisper Transcriber 相同簽名）----

    def transcribe(self, path, on_progress=None) -> str:
        audio_path = self._ensure_audio(Path(path))
        if on_progress:
            on_progress(0.1, "")
        if self._upload or self._generate:  # 測試注入假物件，不經金鑰輪替
            uploaded = self._upload(audio_path)
            text = (self._generate(uploaded) or "").strip()
        else:
            # 上傳的檔案綁在該 key 的專案底下，所以「上傳＋轉錄」必須整組用同一把 key
            text = (
                call_with_rotation(
                    self._pool, lambda key: self._transcribe_with_key(key, audio_path)
                )
                or ""
            ).strip()
        if on_progress:
            on_progress(1.0, text)
        return text

    # ---- 內部 ----

    def _ensure_audio(self, path: Path) -> Path:
        """把非 Gemini 原生格式（影片、webm、m4a…）轉成 wav。"""
        if path.suffix.lower() in _SAFE_AUDIO_EXTS:
            return path
        if media.ffmpeg_available():
            return media.extract_audio(path)
        return path  # 沒有 ffmpeg 就盡力而為，交給 Gemini 自行判斷

    def _client(self, key: str | None):
        if not key:
            raise GeminiTranscribeError(
                "未設定 GEMINI_API_KEY：雲端轉錄需要 Gemini 金鑰，"
                "請在部署平台的環境變數設定 GEMINI_API_KEY"
            )
        from google import genai

        return genai.Client(api_key=key)

    def _transcribe_with_key(self, key: str | None, path: Path) -> str:
        client = self._client(key)
        uploaded = client.files.upload(file=str(path))
        # 音訊通常上傳即就緒；video 或大檔可能要等處理，輪詢到 ACTIVE
        for _ in range(60):
            state = getattr(uploaded, "state", None)
            name = getattr(state, "name", str(state))
            if name == "ACTIVE":
                break
            if name == "FAILED":
                raise GeminiTranscribeError("Gemini 檔案處理失敗，請換一個檔案再試")
            time.sleep(1)
            uploaded = client.files.get(name=uploaded.name)
        response = client.models.generate_content(
            model=self.model,
            contents=[_TRANSCRIBE_PROMPT, uploaded],
            config={"temperature": 0.0},
        )
        return response.text or ""
