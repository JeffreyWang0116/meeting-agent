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


def test_prompt_includes_glossary_terms():
    """自訂詞彙要進轉錄 prompt，人名/專有名詞才不會被聽錯。"""
    t = GeminiTranscriber(
        api_key="k",
        glossary=lambda: [{"term": "王霖翔", "note": "人名"}],
    )
    prompt = t.build_prompt()
    assert "王霖翔（人名）" in prompt
    # 沒有詞彙時維持原本 prompt
    assert "詞彙" not in GeminiTranscriber(api_key="k").build_prompt()


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


def test_transcribe_prompt_asks_for_timestamps():
    """每句要標 [分:秒]：會議重點跳轉與逐字稿時間軸都靠這個。"""
    from app.transcription.gemini_transcriber import _TRANSCRIBE_PROMPT

    assert "[1:02]" in _TRANSCRIBE_PROMPT
    assert "時間" in _TRANSCRIBE_PROMPT


# ---- 長音檔分段轉錄 ----
# 實測整份送出 17 分鐘錄音時，Gemini 會整份放棄講者標註、時間戳也會漂掉；
# 同一支影片只取前 3 分鐘則講者分得又快又準。以下驗證分段機制的縫合行為。

class FakeChunkedSetup:
    """把 media 的長度偵測與切段換成假的，不需要真的 ffmpeg 與音檔。"""

    def __init__(self, monkeypatch, tmp_path, duration, chunk_texts):
        from app.transcription import media

        self.chunk_dir = tmp_path / "src_chunks"
        self.chunk_dir.mkdir()
        self.chunks = []
        for i in range(len(chunk_texts)):
            c = self.chunk_dir / f"chunk_{i:03d}.wav"
            c.write_bytes(b"RIFF-fake")
            self.chunks.append(c)
        self.hints = []

        monkeypatch.setattr(media, "ffmpeg_available", lambda: True)
        monkeypatch.setattr(media, "audio_duration", lambda p: duration)
        monkeypatch.setattr(media, "split_audio", lambda p, secs, output_dir=None: list(self.chunks))

        self.texts = dict(zip((str(c) for c in self.chunks), chunk_texts))

    def transcriber(self, chunk_seconds=240):
        setup = self

        class Recording(GeminiTranscriber):
            def _transcribe_one(self, audio_path, hint):
                setup.hints.append(hint)
                return setup.texts[str(audio_path)]

        return Recording(api_key="k", chunk_seconds=chunk_seconds)


def test_long_audio_is_chunked_and_timestamps_shifted(monkeypatch, tmp_path):
    """第二段的 [0:05] 是它自己的第 5 秒，接回整場後應該變成 [4:05]。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=600, chunk_texts=[
        "[0:05] 講者A：第一段開頭",
        "[0:05] 講者A：第二段開頭",
    ])
    text = setup.transcriber(chunk_seconds=240).transcribe(src)
    assert text == "[0:05] 講者A：第一段開頭\n[4:05] 講者A：第二段開頭"


def test_known_speakers_passed_as_hint_to_later_chunks(monkeypatch, tmp_path):
    """後續段要帶「已出現的講者」提示，否則模型每段都從講者A重新編號。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=600, chunk_texts=[
        "[0:00] 講者A：先講\n[0:30] 講者B：我補充",
        "[0:10] 講者A：再來",
    ])
    setup.transcriber().transcribe(src)
    # 第一段沒有先前的講者可沿用，但仍要收到「務必標註講者」的提示——
    # 開場常是單人發言，模型會依主 prompt 的例外整段不標，導致後續段
    # 拿不到可沿用的講者清單
    assert "不可省略" in setup.hints[0]
    assert "講者A" not in setup.hints[0]
    assert "講者A、講者B" in setup.hints[1]  # 第二段要沿用前面的標籤


def test_short_audio_is_not_chunked(monkeypatch, tmp_path):
    """短音檔整份送出品質就很好，不必多花好幾次 API 請求。"""
    src = tmp_path / "short.wav"
    src.write_bytes(b"RIFF-fake")
    from app.transcription import media

    monkeypatch.setattr(media, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(media, "audio_duration", lambda p: 120)
    called = []
    monkeypatch.setattr(media, "split_audio", lambda *a, **k: called.append(1) or [])

    t = GeminiTranscriber(
        api_key="k", chunk_seconds=240,
        upload=lambda p: {"h": str(p)}, generate=lambda h: "[0:01] 講者A：很短",
    )
    assert t.transcribe(src) == "[0:01] 講者A：很短"
    assert called == []  # 完全沒有動用分段


def test_chunking_disabled_when_chunk_seconds_zero(monkeypatch, tmp_path):
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    from app.transcription import media

    monkeypatch.setattr(media, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(media, "audio_duration", lambda p: 6000)
    called = []
    monkeypatch.setattr(media, "split_audio", lambda *a, **k: called.append(1) or [])

    t = GeminiTranscriber(
        api_key="k", chunk_seconds=0,
        upload=lambda p: {"h": str(p)}, generate=lambda h: "整份轉錄",
    )
    assert t.transcribe(src) == "整份轉錄"
    assert called == []


def test_falls_back_to_whole_file_without_ffmpeg(monkeypatch, tmp_path):
    """沒有 ffmpeg 就切不了段，退回整份轉錄——品質較差但不該整個失敗。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    from app.transcription import media

    monkeypatch.setattr(media, "ffmpeg_available", lambda: False)
    t = GeminiTranscriber(
        api_key="k", chunk_seconds=240,
        upload=lambda p: {"h": str(p)}, generate=lambda h: "整份轉錄",
    )
    assert t.transcribe(src) == "整份轉錄"


def test_split_failure_falls_back_to_whole_file(monkeypatch, tmp_path):
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    from app.transcription import media

    def boom(*a, **k):
        raise media.MediaError("ffmpeg 掛了")

    monkeypatch.setattr(media, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(media, "audio_duration", lambda p: 6000)
    monkeypatch.setattr(media, "split_audio", boom)
    t = GeminiTranscriber(
        api_key="k", chunk_seconds=240,
        upload=lambda p: {"h": str(p)}, generate=lambda h: "整份轉錄",
    )
    assert t.transcribe(src) == "整份轉錄"


def test_empty_chunks_are_skipped(monkeypatch, tmp_path):
    """中間有靜音段轉不出字時，不該在逐字稿留下空行。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=900, chunk_texts=[
        "[0:05] 講者A：有內容", "", "[0:05] 講者A：後面也有",
    ])
    text = setup.transcriber().transcribe(src)
    assert text == "[0:05] 講者A：有內容\n[8:05] 講者A：後面也有"


def test_progress_reported_per_chunk(monkeypatch, tmp_path):
    """長檔要能看到進度與逐段累積的預覽，不是等十幾分鐘才一次跳完。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=900, chunk_texts=[
        "[0:00] 講者A：一", "[0:00] 講者A：二", "[0:00] 講者A：三",
    ])
    calls = []
    setup.transcriber().transcribe(src, on_progress=lambda f, t: calls.append((f, t)))
    assert [round(f, 2) for f, _ in calls] == [0.33, 0.67, 1.0]
    # 第一段之外都要自帶換行，jobs.py 串接預覽時才不會黏成一行
    assert calls[0][1] == "[0:00] 講者A：一"
    assert calls[1][1].startswith("\n")


def test_chunk_files_cleaned_up(monkeypatch, tmp_path):
    """切出來的暫存片段用完要刪掉，長檔會佔不少磁碟。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=600, chunk_texts=[
        "[0:00] 講者A：一", "[0:00] 講者A：二",
    ])
    setup.transcriber().transcribe(src)
    assert not setup.chunk_dir.exists()
