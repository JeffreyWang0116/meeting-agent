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
            def _transcribe_one(self, audio_path, hint, model=None):
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


# ---- 講者標註率過低時重試 ----
# 實測同一段音訊、同一個模型、temperature=0，標註率可能是 26% 也可能是 95%：
# 是執行間的變異，不是音訊太難，所以重跑一次通常就正常了。

class RetryingSetup(FakeChunkedSetup):
    """讓同一段的每次呼叫依序回傳不同結果，模擬模型的執行間變異。"""

    def transcriber_with_sequence(self, sequence, label_retries=1):
        setup = self
        calls = {"n": 0}

        class Sequenced(GeminiTranscriber):
            def _transcribe_one(self, audio_path, hint, model=None):
                text = sequence[min(calls["n"], len(sequence) - 1)]
                calls["n"] += 1
                return text

        setup.calls = calls
        return Sequenced(api_key="k", chunk_seconds=240, label_retries=label_retries)


def test_chunk_with_poor_labels_is_retried(monkeypatch, tmp_path):
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = RetryingSetup(monkeypatch, tmp_path, duration=600, chunk_texts=["", ""])
    # 第一次幾乎沒標籤，第二次正常 → 應採用第二次的結果
    t = setup.transcriber_with_sequence([
        "[0:00] 講者A：有標\n[0:05] 沒標\n[0:10] 沒標\n[0:15] 沒標",
        "[0:00] 講者A：有標\n[0:05] 講者B：也有標",
    ])
    text = t.transcribe(src)
    assert "講者B" in text
    assert setup.calls["n"] >= 2  # 確實重跑了


def test_well_labelled_chunk_is_not_retried(monkeypatch, tmp_path):
    """標得好就不該多花一次 API 請求。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = RetryingSetup(monkeypatch, tmp_path, duration=600, chunk_texts=["", ""])
    t = setup.transcriber_with_sequence(["[0:00] 講者A：一\n[0:05] 講者B：二"])
    t.transcribe(src)
    assert setup.calls["n"] == 2  # 兩段各一次，沒有重試


def test_best_attempt_kept_when_all_retries_are_poor(monkeypatch, tmp_path):
    """重試後仍然不理想時，至少保留標得最好的那一次，而不是最後一次。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = RetryingSetup(monkeypatch, tmp_path, duration=300, chunk_texts=[""])
    t = setup.transcriber_with_sequence([
        "[0:00] 講者A：好一點\n[0:05] 沒標",   # 50%
        "[0:00] 沒標\n[0:05] 沒標",            # 0%
    ], label_retries=1)
    assert "講者A" in t.transcribe(src)


def test_retry_disabled_when_label_retries_zero(monkeypatch, tmp_path):
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = RetryingSetup(monkeypatch, tmp_path, duration=300, chunk_texts=[""])
    t = setup.transcriber_with_sequence(["[0:00] 全部沒標\n[0:05] 也沒標"], label_retries=0)
    t.transcribe(src)
    assert setup.calls["n"] == 1


# ---- 重試用盡後改用較強模型 ----
# 實測同一段連跑兩次都只有 20% 標註率，但換 gemini-flash-latest 就 100%。
# 只在失敗的段落動用，額度較低的模型才不會被整場錄音吃光。

def test_falls_back_to_stronger_model_when_retries_all_fail(monkeypatch, tmp_path):
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    # 分段路徑要 2 段以上才會啟動
    FakeChunkedSetup(monkeypatch, tmp_path, duration=600, chunk_texts=["", ""])
    used_models = []

    class Tracking(GeminiTranscriber):
        def _transcribe_one(self, audio_path, hint, model=None):
            used_models.append(model or self.model)
            if model == "gemini-flash-latest":
                return "[0:00] 講者A：強模型標得好\n[0:05] 講者B：也標了"
            return "[0:00] 沒標\n[0:05] 也沒標"

    # 明確指定參數，不受預設值調整影響（預設的取捨見 config.py）
    t = Tracking(api_key="k", chunk_seconds=240, label_retries=1,
                 model="gemini-flash-lite-latest", fallback_model="gemini-flash-latest",
                 max_fallback_chunks=2)
    text = t.transcribe(src)
    assert "講者A" in text and "講者B" in text
    # 每段：先用便宜模型試 2 次（初次＋重試），失敗才動用強模型 1 次
    per_chunk = ["gemini-flash-lite-latest"] * 2 + ["gemini-flash-latest"]
    assert used_models == per_chunk * 2


def test_stronger_model_not_used_when_labels_are_fine(monkeypatch, tmp_path):
    """標得好就不該動用額度較低的模型。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    FakeChunkedSetup(monkeypatch, tmp_path, duration=600, chunk_texts=["", ""])
    used_models = []

    class Tracking(GeminiTranscriber):
        def _transcribe_one(self, audio_path, hint, model=None):
            used_models.append(model or self.model)
            return "[0:00] 講者A：標得很好\n[0:05] 講者B：也是"

    Tracking(api_key="k", chunk_seconds=240, model="gemini-flash-lite-latest",
             fallback_model="gemini-flash-latest").transcribe(src)
    assert used_models == ["gemini-flash-lite-latest"] * 2  # 兩段各一次，沒動用強模型


def test_fallback_result_discarded_if_worse(monkeypatch, tmp_path):
    """強模型那次反而更差時，保留原本最好的結果。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    FakeChunkedSetup(monkeypatch, tmp_path, duration=600, chunk_texts=["", ""])

    class Tracking(GeminiTranscriber):
        def _transcribe_one(self, audio_path, hint, model=None):
            if model:
                return "[0:00] 完全沒標\n[0:05] 也沒標"
            return "[0:00] 講者A：至少有一行\n[0:05] 沒標"

    text = Tracking(api_key="k", chunk_seconds=240, label_retries=0,
                    fallback_model="gemini-flash-latest").transcribe(src)
    assert "講者A" in text


def test_no_fallback_when_not_configured(monkeypatch, tmp_path):
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    FakeChunkedSetup(monkeypatch, tmp_path, duration=600, chunk_texts=["", ""])
    calls = []

    class Tracking(GeminiTranscriber):
        def _transcribe_one(self, audio_path, hint, model=None):
            calls.append(model)
            return "[0:00] 沒標\n[0:05] 也沒標"

    Tracking(api_key="k", chunk_seconds=240, label_retries=1,
             fallback_model=None).transcribe(src)
    # 每段只有初次＋重試，沒有第三次（兩段共 4 次）
    assert calls == [None] * 4


# ---- 強模型用量上限 ----
# 降級是「每段」獨立判斷的，不設上限的話一個 5 段的難搞檔案就會吃掉 5 次
# 強模型額度（它的每日額度比 lite 低得多）。

def _all_failing(monkeypatch, tmp_path, chunks, **kw):
    """建一個每段都標不好的情境，回傳 (transcriber, 用到的模型清單)。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    FakeChunkedSetup(monkeypatch, tmp_path, duration=chunks * 240, chunk_texts=[""] * chunks)
    used = []

    class Tracking(GeminiTranscriber):
        def _transcribe_one(self, audio_path, hint, model=None):
            used.append(model or self.model)
            return "[0:00] 沒標\n[0:05] 也沒標"

    t = Tracking(api_key="k", chunk_seconds=240, label_retries=1,
                 model="lite", fallback_model="strong", **kw)
    return src, t, used


def test_fallback_capped_per_file(monkeypatch, tmp_path):
    """4 段全部標不好，但最多只有 2 段能動用強模型。"""
    src, t, used = _all_failing(monkeypatch, tmp_path, chunks=4, max_fallback_chunks=2)
    t.transcribe(src)
    assert used.count("strong") == 2
    assert used.count("lite") == 8  # 4 段 × (初次 + 重試)


def test_fallback_cap_can_be_disabled(monkeypatch, tmp_path):
    src, t, used = _all_failing(monkeypatch, tmp_path, chunks=3, max_fallback_chunks=0)
    t.transcribe(src)
    assert used.count("strong") == 0  # 完全不動用強模型


def test_fallback_cap_allows_all_when_high(monkeypatch, tmp_path):
    src, t, used = _all_failing(monkeypatch, tmp_path, chunks=3, max_fallback_chunks=99)
    t.transcribe(src)
    assert used.count("strong") == 3


def test_successful_chunks_do_not_consume_fallback_budget(monkeypatch, tmp_path):
    """標得好的段落不該佔用強模型的配額。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    FakeChunkedSetup(monkeypatch, tmp_path, duration=720, chunk_texts=["", "", ""])
    used = []
    seen = {"n": 0}

    class Tracking(GeminiTranscriber):
        def _transcribe_one(self, audio_path, hint, model=None):
            used.append(model or self.model)
            seen["n"] += 1
            # 第一段標得好，之後兩段都失敗
            if seen["n"] == 1:
                return "[0:00] 講者A：好的\n[0:05] 講者B：也好"
            return "[0:00] 沒標\n[0:05] 也沒標"

    Tracking(api_key="k", chunk_seconds=240, label_retries=1, model="lite",
             fallback_model="strong", max_fallback_chunks=2).transcribe(src)
    assert used.count("strong") == 2  # 兩段失敗的都用得到，額度沒被成功的那段吃掉
