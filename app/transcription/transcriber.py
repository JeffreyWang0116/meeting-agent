"""faster-whisper 包裝：裝置自動偵測、延遲載入、串流進度回報。"""
from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_CUDA_ERROR_KEYWORDS = ("cuda", "cublas", "cudnn")

# GTX 1650 4GB VRAM：medium + float16 是品質與記憶體的平衡點；
# 更大的 VRAM 可在 .env 設 WHISPER_MODEL=large-v3
_DEFAULTS = {
    "cuda": ("medium", "float16"),
    "cpu": ("small", "int8"),
}

# 引導 whisper 輸出繁體中文（否則常出簡體）
_INITIAL_PROMPT = "以下是一場繁體中文與英文夾雜的會議討論。"


def _register_nvidia_dlls() -> None:
    """Windows：pip 裝的 nvidia-cublas/cudnn DLL 不在系統 PATH，
    Python 3.8+ 也不會沿 PATH 找 DLL，必須逐目錄註冊。"""
    if sys.platform != "win32":
        return
    try:
        import nvidia
    except ImportError:
        return
    for pkg_path in nvidia.__path__:
        for bin_dir in Path(pkg_path).glob("*/bin"):
            os.add_dll_directory(str(bin_dir))


def _is_cuda_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(keyword in message for keyword in _CUDA_ERROR_KEYWORDS)


def detect_device() -> str:
    try:
        import ctranslate2

        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        return "cpu"


class Transcriber:
    def __init__(
        self,
        model_size: str | None = None,
        device: str | None = None,
        model=None,
    ):
        self._configured_model = model_size
        self._configured_device = device
        self._model = model  # 測試可直接注入假模型
        self._lock = threading.Lock()
        self.device: str | None = None
        self.model_size: str | None = None

    def _ensure_model(self):
        with self._lock:
            if self._model is not None:
                return self._model

            _register_nvidia_dlls()
            from faster_whisper import WhisperModel

            device = self._configured_device or detect_device()
            default_size, compute_type = _DEFAULTS.get(device, _DEFAULTS["cpu"])
            model_size = self._configured_model or default_size
            try:
                self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
            except Exception as exc:
                if device != "cuda":
                    raise
                # CUDA 環境不完整（缺 cuDNN 等）時退回 CPU，功能不中斷只是變慢
                logger.warning("CUDA 初始化失敗（%s），退回 CPU 模式", exc)
                device = "cpu"
                model_size, compute_type = _DEFAULTS["cpu"]
                self._model = WhisperModel(model_size, device=device, compute_type=compute_type)

            self.device = device
            self.model_size = model_size
            logger.info("Whisper 模型已載入：%s（%s）", model_size, device)
            return self._model

    def _rebuild_on_cpu(self):
        with self._lock:
            from faster_whisper import WhisperModel

            model_size, compute_type = _DEFAULTS["cpu"]
            self._model = WhisperModel(model_size, device="cpu", compute_type=compute_type)
            self.device = "cpu"
            self.model_size = model_size
            logger.info("Whisper 模型已載入：%s（cpu）", model_size)
            return self._model

    def transcribe(self, path, on_progress=None) -> str:
        """轉錄音檔/影片檔，回傳全文。

        on_progress(fraction, segment_text)：每完成一個段落呼叫一次，
        供長檔進度條與部分逐字稿顯示使用。
        """
        model = self._ensure_model()
        try:
            return self._run(model, path, on_progress)
        except (RuntimeError, OSError) as exc:
            # CUDA 函式庫要到實際運算才載入，缺 cublas/cudnn 時
            # 建模不會失敗、轉錄才爆，所以退回 CPU 要放在這裡
            if self.device != "cuda" or not _is_cuda_error(exc):
                raise
            logger.warning("CUDA 執行階段失敗（%s），退回 CPU 重新轉錄", exc)
            return self._run(self._rebuild_on_cpu(), path, on_progress)

    def _run(self, model, path, on_progress) -> str:
        segments, info = model.transcribe(
            str(path),
            vad_filter=True,
            initial_prompt=_INITIAL_PROMPT,
        )
        duration = getattr(info, "duration", 0) or 0
        parts: list[str] = []
        for segment in segments:
            parts.append(segment.text)
            if on_progress and duration:
                on_progress(min(segment.end / duration, 1.0), segment.text)
        return "".join(parts).strip()
