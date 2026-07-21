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
    drop_lines_before,
    normalize_timestamps,
    shift_timestamps,
    speaker_label_ratio,
    transcript_tail,
)

logger = logging.getLogger(__name__)

# Gemini 原生支援的音訊副檔名，這些不需要再轉檔
_SAFE_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".aiff"}

# 超過這個長度就分段轉錄。實測 gemini-flash-lite 餵整份 17 分鐘的質詢錄音時，
# 講者標註會整份消失、時間戳也會漂到比實際長度多 3 分鐘；同一支影片只取前 3
# 分鐘則講者分得又快又準。模型在長音訊上顯然會放棄逐句標註，只能靠分段迴避。
DEFAULT_CHUNK_THRESHOLD_SECONDS = 360  # 6 分鐘

# 一段裡有講者標籤的行數低於這個比例，就當作「模型這輪放棄標講者」而重試。
# 實測同一段快速交鋒的質詢音訊、同一個模型、temperature=0，標註率可能是 26%
# 也可能是 95%——是執行間的變異，不是音訊太難，所以重跑一次通常就正常了。
#
# 訂在 0.8 而非 0.5：prompt 要求每一行都標講者，一半沒標就是模型沒照做。
# 舊值 0.5 是配合當時會灌水的標註率計算訂的——句中冒號（「重點：…」）被誤判
# 成講者，真實標註率再低都能衝過 0.5，重試等於從來沒有真正生效過。
MIN_SPEAKER_LABEL_RATIO = 0.8

# 轉錄 prompt：務必強力要求分辨講者。實測 gemini-flash-lite 在「弱提示」下
# 幾乎不標講者（多人對話被併成一段，使用者只看到講者A/B 甚至沒標），把要求
# 講清楚後，連 lite 模型也能穩定標出講者A/B/C。
#
# 標籤一律用代號，不准用名字：模型沒有跨段記憶，「聽得出名字就用名字」這種
# 條件式指示會讓同一個人在聽得到稱謂的段落標「王委員」、聽不到的段落標
# 「講者A」，整份逐字稿混雜兩種標籤。姓名改由事後對應階段統一填回。
#
# 也不留「可以不標註」的例外：那個例外與分段模式的 chunk_hint 直接矛盾，
# 而不分段的路徑沒有 chunk_hint 抵銷，模型就整份放棄標註。
_TRANSCRIBE_PROMPT = (
    "這是一段可能有多位講者的會議錄音，請完整逐字轉錄成繁體中文。"
    "中英夾雜的地方保留原文（英文單字、技術名詞不要翻譯）。"
    "務必分辨不同說話者：即使聲音、口音或語速相近，只要是不同人輪流發言，"
    "就要分開標註，不要把不同人的話併成同一段。"
    "講者一律用代號「講者A：」「講者B：」「講者C：」依出場順序標註；"
    "即使你從對話中聽得出某人的姓名或職稱，標籤也一律用代號，不要用名字"
    "（姓名會在後續步驟另行對應）。"
    "每一句話（每一行）開頭都必須標註講者，即使整段從頭到尾只有一位講者也不可省略。"
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
        label_retries: int = 2,
        fallback_model: str | None = None,
        max_fallback_chunks: int = 0,
        overlap_seconds: int = 20,
        max_retry_calls: int = 10,
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
        # 一段的講者標註率過低時，最多再重跑幾次（0＝不重試）
        self.label_retries = label_retries
        # 重試用盡仍標不好時，改用這個較強的模型跑最後一次（None＝不啟用）。
        # 只在真的失敗的那一段動用，免費額度較低的模型才不會被整場吃光
        self.fallback_model = fallback_model
        # 單一檔案最多幾段可以動用強模型。免費層實測：Flash Lite 每日 500 次，
        # Flash 只有 20 次——備援跑一次就佔掉每日 Flash 額度的 5%，而 lite 重試
        # 一次只佔 0.2%。預設 0（不啟用）：把預算全花在額度充裕的 lite 重試，
        # 稀有的 Flash 額度留給分析與名字對應
        self.max_fallback_chunks = max_fallback_chunks
        # 單一檔案總共最多幾次重試。只設每段上限的話，總量會隨影片長度線性
        # 膨脹（60 分鐘＝15 段 × 2 次＝30 次額外請求，佔每日額度 6%）；
        # 設每檔上限才能讓重試成本與影片長度脫鉤，維持在額度的 2% 以內
        self.max_retry_calls = max_retry_calls
        # 每段往前多抓幾秒當重疊。模型沒聽過前一段，光給講者名單它無從對應
        # 嗓音，只能從自己這段重新編號——同一個人跨段就會換標籤。重疊讓它
        # 聽得到前一段結尾，配合提示裡的對照樣本才能接得起來
        self.overlap_seconds = overlap_seconds

    def build_prompt(self, hint: str | None = None) -> str:
        prompt = _TRANSCRIBE_PROMPT
        terms = glossary_prompt_line(self._glossary() if self._glossary else [])
        if terms:
            # 詞彙表含人名，要明講它只管內文用字，否則模型會拿它當講者標籤用
            prompt += (
                f"已知詞彙表（聽到相近發音時，人名與專有名詞一律採用以下寫法）：{terms}。"
                "詞彙表只影響內文用字，講者標籤仍一律使用代號。"
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
            chunks = media.split_audio(
                audio_path, self.chunk_seconds, overlap_seconds=self.overlap_seconds
            )
        except media.MediaError as exc:
            # 切不動就照舊整份送出：品質可能較差，但不該讓整個轉錄失敗
            logger.warning("音檔分段失敗（%s），改為整份轉錄", exc)
            return []
        return chunks if len(chunks) > 1 else []

    def _transcribe_chunked(self, chunks, on_progress, hint: str | None) -> str:
        """逐段轉錄再縫合：時間戳平移回整場時間，講者標籤跨段沿用同一組。"""
        parts: list[str] = []
        speakers: list[str] = []
        fallback_used = 0  # 這個檔案已經用掉幾次強模型（受 max_fallback_chunks 限制）
        retries_left = self.max_retry_calls  # 整個檔案共用的重試預算
        chunk_dir = chunks[0].parent
        previous_tail = ""
        try:
            for index, chunk in enumerate(chunks):
                # own_start：本段「負責」的內容從整場的第幾秒開始
                # audio_start：本段音訊實際的起點（第二段起會往前多抓重疊）
                own_start = index * self.chunk_seconds
                audio_start = (
                    own_start if index == 0
                    else max(0, own_start - self.overlap_seconds)
                )
                # 每一段都要帶分段提示（含「即使只有一位講者也要標註」），
                # 並附上已出現的講者清單與重疊處的對照樣本讓標籤接得起來。
                # 第一段若沒標講者，後面就沒有清單可沿用，整條一致性會失效
                this_hint = chunk_hint(speakers, previous_tail)
                if hint:  # 呼叫端另外給的提示（即時聆聽跨 session 用）附在後面
                    this_hint += hint
                text, used_fallback, retries_used = self._transcribe_labelled(
                    chunk,
                    this_hint,
                    allow_fallback=fallback_used < self.max_fallback_chunks,
                    retry_budget=retries_left,
                )
                if used_fallback:
                    fallback_used += 1
                retries_left -= retries_used
                if not text:
                    continue
                text = shift_timestamps(text, audio_start)
                if index:  # 重疊那段前一輪已經轉過了，依絕對時間濾掉避免重複
                    text = drop_lines_before(text, own_start)
                if not text.strip():
                    continue
                collect_speakers(text, speakers)
                previous_tail = transcript_tail(text)
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

    def _transcribe_labelled(
        self,
        chunk: Path,
        hint: str | None,
        allow_fallback: bool = True,
        retry_budget: int | None = None,
    ) -> tuple[str, bool, int]:
        """轉錄一段，講者標註率太低就重跑，取標得最好的那次。

        retry_budget 是整個檔案剩餘的重試次數（None＝不限）；本段最多只能用掉
        這麼多次，長影片才不會因為段數多而讓重試成本線性膨脹。

        回傳 (逐字稿, 有沒有動用強模型, 用掉幾次重試)——呼叫端據此控管整個
        檔案的強模型與重試用量。

        模型偶爾會整段放棄標講者（同一輸入重跑一次就正常），沒有重試的話
        那一段在畫面上就整片沒有講者，前端的續行沿用也會把話歸錯人。
        """
        allowed_retries = self.label_retries
        if retry_budget is not None:
            allowed_retries = min(allowed_retries, max(0, retry_budget))
        best, best_ratio = "", -1.0
        retries_used = 0
        for attempt in range(allowed_retries + 1):
            text = self._transcribe_one(chunk, hint)
            if attempt:
                retries_used += 1
            ratio = speaker_label_ratio(text)
            if ratio > best_ratio:
                best, best_ratio = text, ratio
            if ratio >= MIN_SPEAKER_LABEL_RATIO:
                return best, False, retries_used
            if attempt < allowed_retries:
                logger.info(
                    "%s 講者標註率只有 %.0f%%，重跑一次", chunk.name, ratio * 100
                )

        # 同一個模型重試用盡還是標不好（實測會連兩次都失敗），換較強的模型再試
        # 一次。allow_fallback 由呼叫端控管，避免一個難搞的檔案把強模型額度用光
        if allow_fallback and self.fallback_model and self.fallback_model != self.model:
            logger.info("%s 改用 %s 重跑", chunk.name, self.fallback_model)
            text = self._transcribe_one(chunk, hint, model=self.fallback_model)
            if speaker_label_ratio(text) > best_ratio:
                best = text
            return best, True, retries_used
        return best, False, retries_used

    def _transcribe_one(
        self, audio_path: Path, hint: str | None, model: str | None = None
    ) -> str:
        if self._upload or self._generate:  # 測試注入假物件，不經金鑰輪替
            uploaded = self._upload(audio_path)
            return (self._generate(uploaded) or "").strip()
        # 上傳的檔案綁在該 key 的專案底下，所以「上傳＋轉錄」必須整組用同一把 key
        return (
            call_with_rotation(
                self._pool,
                lambda key: self._transcribe_with_key(key, audio_path, hint, model),
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

    def _transcribe_with_key(
        self,
        key: str | None,
        path: Path,
        hint: str | None = None,
        model: str | None = None,
    ) -> str:
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
                model=model or self.model,
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
