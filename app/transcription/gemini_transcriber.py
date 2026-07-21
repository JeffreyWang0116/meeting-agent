"""用 Gemini 直接聽音訊產生逐字稿（雲端無 GPU 時的轉錄後端）。

介面與本地 faster-whisper 的 Transcriber 完全一致（transcribe / device /
model_size），因此可透過 create_app 的依賴注入直接互換：本地開發用 GPU
Whisper，雲端部署設 TRANSCRIBE_ENGINE=gemini 就換成這個。

流程：把任何輸入（影片、m4a、webm 錄音段）先正規化成 Gemini 支援的音訊格式
（必要時用 ffmpeg 轉 wav），上傳 Files API，再請模型逐字轉錄。
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from app.gemini_keys import KeyPool, call_with_rotation
from app.glossary import glossary_prompt_line
from app.transcription import media
from app.transcription.segments import (
    chunk_hint,
    collect_speakers,
    normalize_timestamps,
    shift_timestamps,
)

logger = logging.getLogger(__name__)

# Gemini 原生支援的音訊副檔名，這些不需要再轉檔
_SAFE_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".aiff"}

# 超過這個長度就分段轉錄。實測 gemini-flash-lite 餵整份 17 分鐘的質詢錄音時，
# 講者標註會整份消失、時間戳也會漂到比實際長度多 3 分鐘；同一支影片只取前 3
# 分鐘則講者分得又快又準。模型在長音訊上顯然會放棄逐句標註，只能靠分段迴避。
DEFAULT_CHUNK_THRESHOLD_SECONDS = 360  # 6 分鐘

# 轉錄 prompt：務必強力要求分辨講者。實測 gemini-flash-lite 在「弱提示」下
# 幾乎不標講者（多人對話被併成一段，使用者只看到講者A/B 甚至沒標），把要求
# 講清楚後，連 lite 模型也能穩定標出講者A/B/C。
_TRANSCRIBE_PROMPT = (
    "這是一段可能有多位講者的會議錄音，請完整逐字轉錄成繁體中文。"
    "中英夾雜的地方保留原文（英文單字、技術名詞不要翻譯）。"
    "務必分辨不同說話者：即使聲音、口音或語速相近，只要是不同人輪流發言，"
    "就在每一句話開頭標註講者——聽得出名字就用名字（如「小明：」），聽不出名字"
    "就用「講者A：」「講者B：」「講者C：」依序區分，不要把不同人的話併成同一段；"
    "只有在整段自始至終確定是同一位講者時，才可以不標註。"
    "每一句話（每一行）開頭標註它在音檔中出現的時間，格式是方括號的分:秒，"
    "例如「[1:02] 講者A：我們開始吧」；超過一小時用 [時:分:秒]。"
    "只輸出逐字稿本身，不要加任何標題或說明。"
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
        glossary=None,
        chunk_seconds: int = 0,
    ):
        # 多把 key 輪替（429 換下一把）；單把 api_key 為向後相容寫法
        self._pool = KeyPool(api_keys if api_keys else [api_key])
        self.api_key = self._pool.first
        self.model = model
        # 供 /api/health 顯示；命名對齊 Whisper Transcriber 以免前端分歧
        self.device = "gemini"
        self.model_size = model
        self._upload = upload
        self._generate = generate
        # callable() -> list[dict]：自訂詞彙表，每次轉錄時讀最新內容
        self._glossary = glossary
        # 每段幾秒；<=0 代表不分段（維持整份送出的舊行為）
        self.chunk_seconds = chunk_seconds

    def build_prompt(self, hint: str | None = None) -> str:
        prompt = _TRANSCRIBE_PROMPT
        terms = glossary_prompt_line(self._glossary() if self._glossary else [])
        if terms:
            prompt += (
                f"已知詞彙表（聽到相近發音時，人名與專有名詞一律採用以下寫法）：{terms}。"
            )
        if hint:  # 跨段講者一致性提示（即時聆聽逐段轉錄時帶入）
            prompt += hint
        return prompt

    # ---- 對外介面（與 Whisper Transcriber 相同簽名）----

    def transcribe(self, path, on_progress=None, hint: str | None = None) -> str:
        audio_path = self._ensure_audio(Path(path))
        chunks = self._plan_chunks(audio_path)
        if chunks:
            return self._transcribe_chunked(chunks, on_progress, hint)

        if on_progress:
            on_progress(0.1, "")
        text = normalize_timestamps(self._transcribe_one(audio_path, hint))
        if on_progress:
            on_progress(1.0, text)
        return text

    # ---- 內部 ----

    def _plan_chunks(self, audio_path: Path) -> list[Path]:
        """長音檔就切段，回傳片段清單；不需要或做不到分段時回傳空 list。"""
        if self.chunk_seconds <= 0 or not media.ffmpeg_available():
            return []
        duration = media.audio_duration(audio_path)
        if duration is None or duration <= DEFAULT_CHUNK_THRESHOLD_SECONDS:
            return []
        try:
            chunks = media.split_audio(audio_path, self.chunk_seconds)
        except media.MediaError as exc:
            # 切不動就照舊整份送出：品質可能較差，但不該讓整個轉錄失敗
            logger.warning("音檔分段失敗（%s），改為整份轉錄", exc)
            return []
        return chunks if len(chunks) > 1 else []

    def _transcribe_chunked(self, chunks, on_progress, hint: str | None) -> str:
        """逐段轉錄再縫合：時間戳平移回整場時間，講者標籤跨段沿用同一組。"""
        parts: list[str] = []
        speakers: list[str] = []
        chunk_dir = chunks[0].parent
        try:
            for index, chunk in enumerate(chunks):
                offset = index * self.chunk_seconds
                # 每一段都要帶分段提示（含「即使只有一位講者也要標註」），
                # 並附上已出現的講者清單讓後續段沿用同一組標籤。
                # 第一段若沒標講者，後面就沒有清單可沿用，整條一致性會失效
                this_hint = chunk_hint(speakers)
                if hint:  # 呼叫端另外給的提示（即時聆聽跨 session 用）附在後面
                    this_hint += hint
                text = self._transcribe_one(chunk, this_hint)
                if not text:
                    continue
                text = shift_timestamps(text, offset)
                collect_speakers(text, speakers)
                parts.append(text)
                if on_progress:
                    # jobs.py 會把每次回報的文字接起來當即時預覽，
                    # 段與段之間要自己補換行才不會黏成一行
                    on_progress(
                        (index + 1) / len(chunks), text if index == 0 else "\n" + text
                    )
        finally:
            shutil.rmtree(chunk_dir, ignore_errors=True)
        return "\n".join(parts).strip()

    def _transcribe_one(self, audio_path: Path, hint: str | None) -> str:
        if self._upload or self._generate:  # 測試注入假物件，不經金鑰輪替
            uploaded = self._upload(audio_path)
            return (self._generate(uploaded) or "").strip()
        # 上傳的檔案綁在該 key 的專案底下，所以「上傳＋轉錄」必須整組用同一把 key
        return (
            call_with_rotation(
                self._pool,
                lambda key: self._transcribe_with_key(key, audio_path, hint),
            )
            or ""
        ).strip()

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

    def _transcribe_with_key(self, key: str | None, path: Path, hint: str | None = None) -> str:
        client = self._client(key)
        uploaded = client.files.upload(file=str(path))
        try:
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
                contents=[self.build_prompt(hint), uploaded],
                config={"temperature": 0.0},
            )
            return response.text or ""
        finally:
            # Files API 有 20GB 儲存上限；即時聆聽每 45 秒上傳一個檔，用完即刪，
            # 不留給 48 小時自動清除（否則長時間聆聽很快撞到上限）
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass
