"""GeminiTranscriber 測試：注入假的 upload/generate，不呼叫真 API。

雲端沒有 GPU，改用 Gemini 直接聽音訊產生逐字稿。介面與本地 Whisper
Transcriber 完全一致（transcribe / device / model_size），可直接互換注入。
"""
from pathlib import Path

import pytest

from app.transcription.gemini_transcriber import (
    GeminiTranscriber,
    GeminiTranscribeError,
)


def test_transcribe_returns_generated_text(tmp_path):
    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"RIFF-fake")
    t = GeminiTranscriber(
        api_key="k",
        upload=lambda p: {"handle": str(p)},
        generate=lambda h: "  這是一段會議逐字稿  ",
    )
    assert t.transcribe(wav) == "這是一段會議逐字稿"


def test_transcribe_prompt_asks_for_speaker_labels():
    """多人會議要標註講者，下游才能自動指派任務負責人。"""
    from app.transcription.gemini_transcriber import _TRANSCRIBE_PROMPT

    assert "講者" in _TRANSCRIBE_PROMPT


def test_progress_callback_reaches_one(tmp_path):
    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"RIFF-fake")
    calls = []
    t = GeminiTranscriber(
        api_key="k",
        upload=lambda p: p,
        generate=lambda h: "文字",
    )
    t.transcribe(wav, on_progress=lambda frac, text: calls.append((frac, text)))
    assert calls[-1][0] == 1.0
    assert calls[-1][1] == "文字"


def test_device_and_model_exposed_for_health():
    t = GeminiTranscriber(api_key="k", model="gemini-3.5-flash")
    assert t.device == "gemini"
    assert t.model_size == "gemini-3.5-flash"


def test_missing_api_key_raises(tmp_path):
    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"RIFF-fake")
    # 未注入 generate/upload，走真實路徑但沒有金鑰 → 立即報錯，不觸網
    t = GeminiTranscriber(api_key=None)
    with pytest.raises(GeminiTranscribeError):
        t.transcribe(wav)


def test_non_audio_format_is_converted(tmp_path, monkeypatch):
    from app.transcription import media

    converted = tmp_path / "chunk_audio.wav"
    converted.write_bytes(b"wav")
    monkeypatch.setattr(media, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(media, "extract_audio", lambda src, dst=None: converted)

    chunk = tmp_path / "chunk.webm"
    chunk.write_bytes(b"webm-bytes")

    uploaded_paths = []
    t = GeminiTranscriber(
        api_key="k",
        upload=lambda p: uploaded_paths.append(Path(p)) or {"h": 1},
        generate=lambda h: "轉出來的字",
    )
    assert t.transcribe(chunk) == "轉出來的字"
    # webm 不是 Gemini 支援格式，應先被轉成 wav 再上傳
    assert uploaded_paths == [converted]


def test_wav_is_not_reconverted(tmp_path, monkeypatch):
    from app.transcription import media

    def _boom(*a, **k):
        raise AssertionError("wav 不該被再轉一次")

    monkeypatch.setattr(media, "extract_audio", _boom)
    wav = tmp_path / "already.wav"
    wav.write_bytes(b"RIFF")

    uploaded = []
    t = GeminiTranscriber(
        api_key="k",
        upload=lambda p: uploaded.append(Path(p)) or {},
        generate=lambda h: "x",
    )
    t.transcribe(wav)
    assert uploaded == [wav]
