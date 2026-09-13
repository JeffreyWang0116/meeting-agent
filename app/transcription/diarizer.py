"""Diarizer：把 pyannote 講者分離接進轉錄工作。

- start()：壓縮＋上傳＋分群丟到背景，與 Gemini 轉錄並行——PoC 裡上傳加分群
  只要一分鐘，轉錄要七八分鐘，排在後面等於白等
- apply()：轉錄完成後拿分群結果重標逐字稿。**任何失敗都原樣回傳**：講者辨識
  是加分項，pyannote 當掉、額度用完、ffmpeg 壓不動，都退回 Gemini 自己標的代號
"""
from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from app.transcription import media
from app.transcription.pyannote_client import PyannoteClient
from app.transcription.speaker_align import relabel

logger = logging.getLogger(__name__)

# 同時進行的分群工作上限。瓶頸在上傳頻寬與 pyannote 端，不在本機 CPU
_MAX_PARALLEL_JOBS = 2


class Diarizer:
    def __init__(
        self,
        client: PyannoteClient,
        on_call: Callable[[], None] | None = None,
        encode: Callable[[Path], Path] = media.encode_for_diarization,
        duration: Callable[[Path], float | None] = media.audio_duration,
        submit: Callable[[Callable[[], dict]], Future] | None = None,
    ):
        self.client = client
        self._on_call = on_call
        self._encode = encode
        self._duration = duration
        self._submit = submit or ThreadPoolExecutor(
            max_workers=_MAX_PARALLEL_JOBS, thread_name_prefix="diarize"
        ).submit

    def start(self, audio_path: Path | str) -> Future:
        """背景開始分群，回傳 Future（結果是 (segments, 音檔長度)）。"""
        return self._submit(lambda: self._run(Path(audio_path)))

    def apply(self, future: Future | None, transcript: str) -> str:
        try:
            segments, duration = future.result()
        except Exception as exc:
            logger.warning("講者分離失敗，沿用 Gemini 標註的代號：%s", exc)
            return transcript
        if not segments:
            return transcript
        text, stats = relabel(transcript, segments, duration)
        logger.info("講者分離對齊：%s", stats)
        return text

    def _run(self, audio_path: Path) -> tuple[list[dict], float | None]:
        # 音檔長度用原檔量：丟掉「時間戳超出音檔長度」的幻覺行靠的就是它
        duration = self._duration(audio_path)
        encoded = self._encode(audio_path)
        try:
            if self._on_call:
                self._on_call()
            output = self.client.diarize(encoded)
        finally:
            Path(encoded).unlink(missing_ok=True)
        segments = output.get("exclusiveDiarization") or output.get("diarization") or []
        return segments, duration


def build_diarizer(settings, on_call: Callable[[], None] | None = None) -> Diarizer | None:
    """有金鑰、且轉錄引擎是 Gemini 才建。

    本地 Whisper 的逐字稿沒有時間戳，分群結果對不回任何一行，送去只是白花試用額度。
    """
    if not settings.pyannote_api_key or settings.transcribe_engine != "gemini":
        return None
    client = PyannoteClient(
        settings.pyannote_api_key,
        model=settings.pyannote_model,
        timeout_seconds=settings.diarize_timeout_seconds,
    )
    return Diarizer(client, on_call=on_call)
