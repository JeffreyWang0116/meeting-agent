"""GeminiTranscriber 測試：注入假的 upload/generate，不呼叫真 API。"""
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


def test_prompt_keeps_english_only_for_spoken_english():
    """「中英夾雜保留原文」那句會讓模型聽不清楚時改寫英文：立法院質詢「謝謝副院長，
    請卓院長」被轉成「謝謝副Ping，我們現在請左Ping」。英文只能用在真的說了英文的地方。"""
    from app.transcription.gemini_transcriber import _TRANSCRIBE_PROMPT

    assert "聽不清楚的中文不要用英文字或拼音代替" in _TRANSCRIBE_PROMPT


def test_prompt_forbids_using_names_as_speaker_labels():
    """標籤一律用代號。模型沒有跨段記憶，允許它「聽得出名字就用名字」的話，
    同一個人在聽得到稱謂的段落標「王委員」、聽不到的段落標「講者A」，
    使用者看到的就是同一場會議裡混雜姓名與代號。"""
    from app.transcription.gemini_transcriber import _TRANSCRIBE_PROMPT

    assert "不要用名字" in _TRANSCRIBE_PROMPT
    assert "講者A" in _TRANSCRIBE_PROMPT


def test_prompt_has_no_opt_out_from_labelling():
    """舊 prompt 說「整段同一人可以不標註」，chunk_hint 又說「務必標註」，
    同一份 prompt 裡兩句話打架。而且不分段的路徑沒有 chunk_hint 抵銷，
    模型就整份不標講者——使用者看到的「全部都沒標示」由此而來。"""
    from app.transcription.gemini_transcriber import _TRANSCRIBE_PROMPT

    assert "才可以不標註" not in _TRANSCRIBE_PROMPT
    assert "不可省略" in _TRANSCRIBE_PROMPT


def test_prompt_includes_glossary_terms():
    """自訂詞彙要進轉錄 prompt，人名/專有名詞才不會被聽錯。"""
    t = GeminiTranscriber(
        api_key="k",
        glossary=lambda user=None: [{"term": "林佳蓉", "note": "人名"}],
    )
    prompt = t.build_prompt()
    assert "林佳蓉（人名）" in prompt
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


def test_medium_file_uses_lite_chunking(monkeypatch, tmp_path):
    """超過 6 分鐘的檔一律用 lite 分段，每段一次。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    FakeChunkedSetup(monkeypatch, tmp_path, duration=480, chunk_texts=["a", "b"])
    used = []

    class Tracking(GeminiTranscriber):
        def _transcribe_one(self, audio_path, hint, on_partial=None,
                            voice_refs=None, user=None):
            used.append(self.model)
            return "[0:00] 講者A：內容"

    Tracking(api_key="k", chunk_seconds=240, model="gemini-flash-lite-latest").transcribe(src)
    assert used == ["gemini-flash-lite-latest"] * 2


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
        monkeypatch.setattr(
            media, "split_audio",
            lambda p, secs, output_dir=None, overlap_seconds=0: list(self.chunks),
        )

        self.texts = dict(zip((str(c) for c in self.chunks), chunk_texts))

    def transcriber(self, chunk_seconds=240, overlap_seconds=0):
        """overlap_seconds 預設 0：多數測試要隔離的是縫合邏輯本身，
        重疊行為另有專屬測試。"""
        setup = self

        class Recording(GeminiTranscriber):
            def _transcribe_one(self, audio_path, hint, model=None, on_partial=None,
                            voice_refs=None, user=None):
                setup.hints.append(hint)
                result = setup.texts[str(audio_path)]
                if isinstance(result, Exception):  # 讓測試能指定「這一段轉錄失敗」
                    raise result
                return result

        return Recording(
            api_key="k", chunk_seconds=chunk_seconds, overlap_seconds=overlap_seconds
        )


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


def _overload_exc():
    return RuntimeError(
        "503 UNAVAILABLE. {'error': {'code': 503, 'message': "
        "'This model is currently experiencing high demand.'}}"
    )


def test_one_failed_chunk_does_not_discard_the_whole_transcript(monkeypatch, tmp_path):
    """一段撞到 503 就整份報錯的話，前面已經轉好的段落全部作廢、使用者白等
    十分鐘。缺一段可以接受，白等一場然後什麼都沒有不行。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=900, chunk_texts=[
        "[0:05] 講者A：第一段",
        _overload_exc(),
        "[0:05] 講者A：第三段",
    ])
    text = setup.transcriber(chunk_seconds=240).transcribe(src)
    assert "第一段" in text and "第三段" in text
    assert "[8:05] 講者A：第三段" in text  # 中間那段失敗不該讓後面的時間戳錯位


def test_failed_chunk_leaves_a_visible_gap_marker(monkeypatch, tmp_path):
    """缺漏要標出來。少了標記，使用者看到的是一份「看起來完整」但中間憑空
    少了四分鐘的逐字稿，而且無從察覺。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=900, chunk_texts=[
        "[0:05] 講者A：第一段",
        _overload_exc(),
        "[0:05] 講者A：第三段",
    ])
    text = setup.transcriber(chunk_seconds=240).transcribe(src)
    marker = [ln for ln in text.split("\n") if "轉錄失敗" in ln]
    assert len(marker) == 1
    assert marker[0].startswith("[4:00]")  # 缺漏區間的起點（整場絕對時間）


def test_chunk_pipeline_is_logged_so_the_zero_percent_phase_is_legible(
    monkeypatch, tmp_path, caplog
):
    """進度條要等到第一段吐出時間戳才離開 0%，在那之前（切段、上傳、等首個
    回應）畫面完全靜止。30 分鐘的檔這段可能好幾分鐘，logs 一片空白時無從
    分辨是在跑還是已經死了。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=600, chunk_texts=[
        "[0:05] 講者A：第一段",
        "[0:05] 講者A：第二段",
    ])
    with caplog.at_level("INFO", logger="app.transcription.gemini_transcriber"):
        setup.transcriber(chunk_seconds=240).transcribe(src)
    msgs = [
        r.getMessage() for r in caplog.records
        if r.name == "app.transcription.gemini_transcriber"
    ]
    assert any("2 段" in m for m in msgs), "要講清楚切成幾段，才知道總共要等幾輪"
    assert sum("1/2" in m for m in msgs) == 1  # 每段開始各記一行
    assert sum("2/2" in m for m in msgs) == 1


def test_chunk_after_a_failure_is_not_told_it_overlaps(monkeypatch, tmp_path):
    """重疊對照樣本是「你這段開頭就是這些內容，請比對聲音接回同一個標籤」。
    中間那段整段沒轉出來時，那份樣本跟本段音訊差了四分鐘，照舊送出就是叫模型
    拿對不上的文字去配聲音。講者清單仍然有效，只有重疊的宣稱要拿掉。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=900, chunk_texts=[
        "[0:05] 講者A：第一段\n[0:30] 講者B：我補充",
        _overload_exc(),
        "[0:05] 講者A：第三段",
    ])
    setup.transcriber(chunk_seconds=240).transcribe(src)
    assert "與前一段重疊" not in setup.hints[2]
    assert "講者A、講者B" in setup.hints[2]  # 講者清單不受影響，仍要沿用


def test_all_chunks_failing_still_raises(monkeypatch, tmp_path):
    """全部都失敗時沒有任何內容可交，要照舊報錯——不能回一份只有缺漏標記的
    逐字稿，那會被當成「轉錄成功但整場沒人說話」。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=900, chunk_texts=[
        _overload_exc(), _overload_exc(), _overload_exc(),
    ])
    with pytest.raises(RuntimeError, match="UNAVAILABLE"):
        setup.transcriber(chunk_seconds=240).transcribe(src)


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

    def transcriber_with_sequence(self, sequence, label_retries=1, **kwargs):
        setup = self
        calls = {"n": 0}

        class Sequenced(GeminiTranscriber):
            def _transcribe_one(self, audio_path, hint, model=None, on_partial=None,
                            voice_refs=None, user=None):
                text = sequence[min(calls["n"], len(sequence) - 1)]
                calls["n"] += 1
                return text

        setup.calls = calls
        return Sequenced(
            api_key="k", chunk_seconds=240, label_retries=label_retries, **kwargs
        )


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


def test_empty_content_labels_trigger_retry(monkeypatch, tmp_path):
    """整段只有時間戳與空「講者A：」（模型放棄轉錄內容）也要重試。
    實測台語質詢有整個 chunk 這樣，舊版把空標籤算成標好而不重試。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = RetryingSetup(monkeypatch, tmp_path, duration=600, chunk_texts=["", ""])
    t = setup.transcriber_with_sequence([
        "[0:00] 講者A：\n[0:05] 講者A：",           # 有標籤但沒內容 → 應判定失敗
        "[0:00] 講者A：有內容\n[0:05] 講者B：也有",   # 正常
    ])
    text = t.transcribe(src)
    assert "有內容" in text
    assert setup.calls["n"] >= 3  # 第一段重跑過


def test_empty_label_lines_never_reach_output(monkeypatch, tmp_path):
    """就算重試後仍有殘留的空標籤行，也不該出現在最終逐字稿。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = RetryingSetup(monkeypatch, tmp_path, duration=600, chunk_texts=["", ""])
    t = setup.transcriber_with_sequence(
        ["[0:00] 講者A：真的有講\n[0:05] 講者A：\n[0:09] 講者A：又講"],
        label_retries=0,
    )
    text = t.transcribe(src)
    # 中間那行空標籤要被清掉，剩下的兩行內容都在
    assert "真的有講" in text and "又講" in text
    assert "講者A：\n" not in text and not text.endswith("講者A：")


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


def test_half_labelled_chunk_is_retried(monkeypatch, tmp_path):
    """門檻 0.8：prompt 要求每一行都標講者，一半沒標就是模型沒照做。

    舊門檻 0.5 是配合會灌水的標註率計算訂的——句中冒號被誤判成講者，
    真實標註率再低都能衝過 0.5。計算修正後，門檻要跟著回到真正的標準。
    """
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = RetryingSetup(monkeypatch, tmp_path, duration=600, chunk_texts=["", ""])
    t = setup.transcriber_with_sequence([
        "[0:00] 講者A：有標\n[0:05] 沒標",          # 50% → 舊門檻會放行
        "[0:00] 講者A：有標\n[0:05] 講者B：也有標",  # 100%
    ])
    assert "講者B" in t.transcribe(src)
    assert setup.calls["n"] >= 3  # 第一段重跑過（否則兩段只會有 2 次）


# ---- 有 pyannote 時不為講者標籤重跑 ----
# 實測 10.5 分鐘質詢：3 段全因標註率低各重跑 2 次，9 次呼叫 114 秒——但講者最後會被
# pyannote 依時間戳整份重標，Gemini 的標籤根本用不到。只留「整段放棄轉錄」的防護。

def test_missing_labels_are_not_retried_when_diarizer_relabels(monkeypatch, tmp_path):
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = RetryingSetup(monkeypatch, tmp_path, duration=600, chunk_texts=["", ""])
    t = setup.transcriber_with_sequence(
        ["[0:00] 沒標\n[0:05] 也沒標"], label_retries=2, relabeled_later=True
    )
    assert "也沒標" in t.transcribe(src)
    assert setup.calls["n"] == 2  # 兩段各一次


def test_abandoned_content_is_still_retried_when_diarizer_relabels(monkeypatch, tmp_path):
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = RetryingSetup(monkeypatch, tmp_path, duration=600, chunk_texts=["", ""])
    t = setup.transcriber_with_sequence(
        ["[0:00] 講者A：\n[0:05] 講者A：。", "[0:00] 有內容\n[0:05] 沒標也沒關係"],
        label_retries=2, relabeled_later=True,
    )
    assert "有內容" in t.transcribe(src)
    assert setup.calls["n"] == 3  # 第一段重跑一次，第二段一次


def test_missing_timestamps_are_still_retried_when_diarizer_relabels(monkeypatch, tmp_path):
    """沒有時間戳的行 pyannote 對不上，等於講者全部歸屬不明。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = RetryingSetup(monkeypatch, tmp_path, duration=600, chunk_texts=["", ""])
    t = setup.transcriber_with_sequence(
        ["講者A：整段沒時間\n講者B：也沒有", "[0:00] 有時間\n[0:05] 也有"],
        label_retries=2, relabeled_later=True,
    )
    assert "有時間" in t.transcribe(src)
    assert setup.calls["n"] == 3  # 第一段重跑一次，第二段一次


# ---- 每檔重試次數上限 ----
# 重試是「每段」獨立判斷的，只設每段上限的話，總量會隨影片長度線性膨脹：
# 一支 60 分鐘的質詢影片切成 15 段，每段重試 2 次就是 30 次額外請求，
# 佔掉 Flash Lite 每日 500 次額度的 6%。設每檔上限才能讓成本與長度脫鉤。

def test_retry_calls_are_capped_per_file(monkeypatch, tmp_path):
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = RetryingSetup(monkeypatch, tmp_path, duration=1200, chunk_texts=[""] * 5)
    t = setup.transcriber_with_sequence(
        ["[0:00] 沒標\n[0:05] 也沒標"], label_retries=2, max_retry_calls=2
    )
    t.transcribe(src)
    # 5 段基礎 + 上限 2 次重試 = 7，而不是 5 + 5×2 = 15
    assert setup.calls["n"] == 7


def test_retry_budget_is_spent_on_earliest_failing_chunks(monkeypatch, tmp_path):
    """預算用完後，後面的段落只跑基礎那次，不會整個檔案失敗。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = RetryingSetup(monkeypatch, tmp_path, duration=1200, chunk_texts=[""] * 5)
    t = setup.transcriber_with_sequence(
        ["[0:00] 沒標\n[0:05] 也沒標"], label_retries=2, max_retry_calls=0
    )
    text = t.transcribe(src)
    assert setup.calls["n"] == 5   # 完全不重試
    assert text                     # 但仍然產出逐字稿


def test_retry_disabled_when_label_retries_zero(monkeypatch, tmp_path):
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = RetryingSetup(monkeypatch, tmp_path, duration=300, chunk_texts=[""])
    t = setup.transcriber_with_sequence(["[0:00] 全部沒標\n[0:05] 也沒標"], label_retries=0)
    t.transcribe(src)
    assert setup.calls["n"] == 1


# ---- 重疊分段（跨段講者一致性）----
# 模型沒聽過前一段，光給講者名單無從對應嗓音，同一個人跨段就會換標籤。
# 每段往前多抓一小段音訊，並把前一段結尾的逐字稿當對照樣本給它。

def test_overlap_region_is_deduplicated(monkeypatch, tmp_path):
    """第二段往前多抓 20 秒，那段內容前一輪已經轉過，不該重複出現。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=600, chunk_texts=[
        "[0:05] 講者A：第一段內容",
        # 這段音訊從 3:40 開始（240-20），所以 [0:10] = 3:50 落在重疊區，
        # [0:30] = 4:10 才是本段真正負責的內容
        "[0:10] 講者A：重疊區重複的話\n[0:30] 講者B：本段真正的內容",
    ])
    text = setup.transcriber(chunk_seconds=240, overlap_seconds=20).transcribe(src)
    assert "重疊區重複的話" not in text
    assert "[4:10] 講者B：本段真正的內容" in text
    assert "[0:05] 講者A：第一段內容" in text


def test_previous_tail_passed_to_next_chunk_hint(monkeypatch, tmp_path):
    """後續段的提示要帶前一段結尾的逐字稿，模型才能把聲音對回既有標籤。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=600, chunk_texts=[
        "[0:05] 講者A：開頭\n[3:55] 講者C：第一段的最後一句",
        "[0:30] 講者C：接下去講",
    ])
    setup.transcriber(chunk_seconds=240, overlap_seconds=20).transcribe(src)
    assert "第一段的最後一句" in setup.hints[1]
    assert "不要重新編號" in setup.hints[1]
    assert "重疊" not in setup.hints[0]  # 第一段沒有前文可對照


def test_overlap_disabled_keeps_plain_offsets(monkeypatch, tmp_path):
    """overlap=0 時回到單純的分段位移，不做去重。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=600, chunk_texts=[
        "[0:05] 講者A：一", "[0:05] 講者A：二",
    ])
    text = setup.transcriber(chunk_seconds=240, overlap_seconds=0).transcribe(src)
    assert text == "[0:05] 講者A：一\n[4:05] 講者A：二"


def test_chunk_fully_inside_overlap_is_skipped(monkeypatch, tmp_path):
    """整段內容都落在重疊區時（模型只轉出重複的部分），跳過而不是留空行。"""
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=600, chunk_texts=[
        "[0:05] 講者A：第一段",
        "[0:05] 講者A：全都在重疊區",  # 3:45，早於 4:00
    ])
    text = setup.transcriber(chunk_seconds=240, overlap_seconds=20).transcribe(src)
    assert text == "[0:05] 講者A：第一段"
    assert not text.endswith("\n")


# ---- 串流轉錄：進度要一路動，不能卡在 10% 再跳完成 ----

def _short_file(monkeypatch, tmp_path, duration=180):
    from app.transcription import media

    monkeypatch.setattr(media, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(media, "audio_duration", lambda p: duration)
    src = tmp_path / "clip.wav"
    src.write_bytes(b"RIFF-fake")
    return src


def test_short_file_reports_progress_while_streaming(monkeypatch, tmp_path):
    """六分鐘以內的檔案不分段，整份走一次大呼叫。

    原本只回報 0.1 然後一路阻塞到結束才 1.0——使用者看到的是進度條卡在 10%
    幾分鐘，然後瞬間完成，完全無從判斷是在跑還是掛了。改成串流後，模型每吐
    一段就依「最後一個時間戳 ÷ 音檔長度」回報真實進度。
    """
    src = _short_file(monkeypatch, tmp_path, duration=180)
    pieces = ["[0:00] 講者A：開始\n", "[1:00] 講者B：中段\n", "[2:30] 講者A：結尾\n"]
    seen = []

    t = GeminiTranscriber(
        api_key="k",
        upload=lambda p: {"h": 1},
        generate=lambda h: iter(pieces),  # 產生器＝模擬串流
        chunk_seconds=240,
    )
    text = t.transcribe(src, on_progress=lambda f, txt: seen.append((f, txt)))

    fractions = [f for f, _ in seen]
    assert fractions == sorted(fractions), f"進度不能倒退：{fractions}"
    middle = [f for f in fractions if 0.1 < f < 1.0]
    assert len(middle) >= 2, f"串流中要有多個中間進度，不能 10%→完成：{fractions}"
    assert "結尾" in text


def test_streaming_surfaces_partial_transcript(monkeypatch, tmp_path):
    """進度數字之外，逐字稿本身也要邊轉邊出現——那比百分比更能讓人安心。"""
    src = _short_file(monkeypatch, tmp_path, duration=180)
    seen = []

    t = GeminiTranscriber(
        api_key="k",
        upload=lambda p: {"h": 1},
        generate=lambda h: iter(["[0:00] 講者A：開始\n", "[1:00] 講者B：中段\n"]),
        chunk_seconds=240,
    )
    t.transcribe(src, on_progress=lambda f, txt: seen.append((f, txt)))

    partials = [txt for f, txt in seen if 0.1 < f < 1.0]
    assert any("中段" in x for x in partials), f"串流中看不到部分逐字稿：{seen}"


def test_plain_string_generate_still_works(monkeypatch, tmp_path):
    """注入的假 generate 回傳字串（非產生器）時照舊運作，不強迫改寫。"""
    src = _short_file(monkeypatch, tmp_path, duration=180)
    t = GeminiTranscriber(
        api_key="k", upload=lambda p: {"h": 1},
        generate=lambda h: "[0:00] 講者A：一次講完", chunk_seconds=240,
    )
    assert "一次講完" in t.transcribe(src)


def test_chunked_progress_moves_within_a_chunk(monkeypatch, tmp_path):
    """長檔逐段轉錄，段與段之間隔數十秒。段內也要回報進度，否則一小時的檔案
    就是 15 次跳動、中間一直不動——看起來仍像卡住。

    段內只推進度數字、不送預覽文字：預覽文字由每段完成時送出（jobs.py 會把
    每次回報的文字接起來），段內再送一份會重複計算。
    """
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    FakeChunkedSetup(monkeypatch, tmp_path, duration=480, chunk_texts=["a", "b"])
    calls = []

    class Streaming(GeminiTranscriber):
        def _transcribe_one(self, audio_path, hint, model=None, on_partial=None,
                            voice_refs=None, user=None):
            if on_partial:
                on_partial("[1:00] 講者A：前半")
                on_partial("[1:00] 講者A：前半\n[3:00] 講者A：後半")
            # 時間戳是段內相對時間，要避開重疊區（第二段起前 20 秒被濾掉）
            return "[2:00] 講者A：內容"

    t = Streaming(api_key="k", chunk_seconds=240)
    t.transcribe(src, on_progress=lambda f, txt: calls.append((round(f, 2), txt)))

    fractions = [f for f, _ in calls]
    assert fractions == sorted(fractions), f"進度不能倒退：{fractions}"
    assert any(0 < f < 0.5 for f in fractions), f"第一段段內沒有進度：{fractions}"
    assert [f for f, _ in calls if f in (0.5, 1.0)] == [0.5, 1.0], "每段完成仍要回報"
    assert all(txt == "" for f, txt in calls if f not in (0.5, 1.0)), "段內不該送預覽文字"


def test_transcriber_reports_every_api_call(monkeypatch, tmp_path):
    """一個長檔會打很多次 API，用量統計要算到每一次。

    原本統計記在端點層：上傳一個檔案記 1 次。但 15 分鐘的檔會切成 4 段、
    各打一次，畫面顯示的數字因此只有真實用量的四分之一——拿來判斷「今天
    還剩多少免費額度」等於看錯表。
    """
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    FakeChunkedSetup(monkeypatch, tmp_path, duration=900, chunk_texts=["a", "b", "c", "d"])
    calls = []

    t = GeminiTranscriber(api_key="k", chunk_seconds=240, on_call=lambda: calls.append(1))
    monkeypatch.setattr(
        t, "_transcribe_with_key",
        lambda key, path, hint=None, model=None, on_partial=None, voice_refs=None,
            user=None: "[0:10] 講者A：內容",
    )
    t.transcribe(src)

    assert len(calls) == 4, f"四段應各記一次，實際 {len(calls)}"


# ---- 聲紋跨段接力（approach A）：分段轉錄邊轉邊建聲音簿，接力餵給後續分段 ----


def test_voice_relay_grows_book_and_passes_refs_to_next_chunk(monkeypatch, tmp_path):
    """新代號的聲音樣本剪出來後，從下一段開始當參考音訊接力餵給模型。"""
    from app.transcription import media

    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=600, chunk_texts=[
        "[0:00] 講者A：第一段開頭發言內容比較長一點才挑得到樣本",
        "[0:00] 講者A：延續\n[0:20] 講者B：新的人",
    ])

    cut_calls = []

    def fake_cut_clip(path, start, end, output_path=None):
        cut_calls.append((path, start, end))
        return Path(f"{path}.clip{len(cut_calls)}.wav")

    monkeypatch.setattr(media, "cut_clip", fake_cut_clip)
    captured_refs = []

    class Recording(GeminiTranscriber):
        def _transcribe_one(self, audio_path, hint, model=None, on_partial=None,
                            voice_refs=None, user=None):
            captured_refs.append(voice_refs)
            return setup.texts[str(audio_path)]

    Recording(api_key="k", chunk_seconds=240, voice_relay_max_speakers=20, overlap_seconds=0).transcribe(src)

    assert captured_refs[0] is None  # 第一段還沒有任何樣本可用
    assert {r["label"] for r in captured_refs[1]} == {"講者A"}  # 第一段確立的講者
    assert len(cut_calls) == 2  # 講者A（第一段後）、講者B（第二段後）各剪一次


def test_voice_relay_disabled_by_default_never_cuts_clips(monkeypatch, tmp_path):
    """預設 voice_relay_max_speakers=0：功能停用不該產生任何額外的 ffmpeg 呼叫。"""
    from app.transcription import media

    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=600, chunk_texts=[
        "[0:00] 講者A：開頭",
        "[0:00] 講者B：後段",
    ])
    cut_calls = []
    monkeypatch.setattr(
        media, "cut_clip",
        lambda *a, **k: cut_calls.append(1) or Path("unused.wav"),
    )
    captured_refs = []

    class Recording(GeminiTranscriber):
        def _transcribe_one(self, audio_path, hint, model=None, on_partial=None,
                            voice_refs=None, user=None):
            captured_refs.append(voice_refs)
            return setup.texts[str(audio_path)]

    Recording(api_key="k", chunk_seconds=240).transcribe(src)  # 預設值，未指定

    assert cut_calls == []
    assert all(refs is None for refs in captured_refs)


def test_voice_relay_skipped_when_speakers_are_relabeled_later(monkeypatch, tmp_path):
    """有 pyannote 時接力標出的代號最後會被整份重標蓋掉，剪樣本、重傳樣本都白付。
    即使設定開著也不剪——判斷集中在轉錄器，呼叫端只講「之後會重標」。"""
    from app.transcription import media

    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=600, chunk_texts=[
        "[0:00] 講者A：第一段開頭發言內容比較長一點才挑得到樣本",
        "[0:00] 講者B：後段",
    ])
    cut_calls = []
    monkeypatch.setattr(
        media, "cut_clip",
        lambda *a, **k: cut_calls.append(1) or Path("unused.wav"),
    )
    captured_refs = []

    class Recording(GeminiTranscriber):
        def _transcribe_one(self, audio_path, hint, on_partial=None,
                            voice_refs=None, user=None):
            captured_refs.append(voice_refs)
            return setup.texts[str(audio_path)]

    Recording(
        api_key="k", chunk_seconds=240, voice_relay_max_speakers=20,
        overlap_seconds=0, relabeled_later=True,
    ).transcribe(src)

    assert cut_calls == []
    assert all(refs is None for refs in captured_refs)


def test_voice_relay_caps_at_max_speakers(monkeypatch, tmp_path):
    """封頂之後不再替新代號剪樣本，但既有樣本繼續沿用。"""
    from app.transcription import media

    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=900, chunk_texts=[
        "[0:00] 講者A：第一位發言內容足夠長",
        "[0:00] 講者B：第二位發言內容也足夠長",
        "[0:00] 講者C：第三位應該被擋在封頂之外",
    ])
    cut_calls = []

    def fake_cut_clip(path, start, end, output_path=None):
        cut_calls.append(1)
        return Path(f"{path}.clip{len(cut_calls)}.wav")

    monkeypatch.setattr(media, "cut_clip", fake_cut_clip)
    captured_refs = []

    class Recording(GeminiTranscriber):
        def _transcribe_one(self, audio_path, hint, model=None, on_partial=None,
                            voice_refs=None, user=None):
            captured_refs.append(voice_refs)
            return setup.texts[str(audio_path)]

    Recording(api_key="k", chunk_seconds=240, voice_relay_max_speakers=2, overlap_seconds=0).transcribe(src)

    assert len(cut_calls) == 2  # 講者A、講者B 各一次；講者C 被擋下
    # 第三段開始前，聲音簿已滿在講者A／B，講者C 不會出現在任何一次的 refs 裡
    assert all(
        refs is None or all(r["label"] != "講者C" for r in refs) for refs in captured_refs
    )


def test_voice_relay_skips_speaker_when_clip_cut_fails(monkeypatch, tmp_path):
    """剪音檔失敗（ffmpeg 出錯）不該讓整個轉錄跟著失敗，只是那個代號沒有樣本。"""
    from app.transcription import media

    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=600, chunk_texts=[
        "[0:00] 講者A：第一段內容足夠長才挑得到樣本",
        "[0:00] 講者A：第二段",
    ])

    def failing_cut_clip(path, start, end, output_path=None):
        raise media.MediaError("seek 失敗")

    monkeypatch.setattr(media, "cut_clip", failing_cut_clip)
    captured_refs = []

    class Recording(GeminiTranscriber):
        def _transcribe_one(self, audio_path, hint, model=None, on_partial=None,
                            voice_refs=None, user=None):
            captured_refs.append(voice_refs)
            return setup.texts[str(audio_path)]

    text = Recording(
        api_key="k", chunk_seconds=240, voice_relay_max_speakers=20, overlap_seconds=0
    ).transcribe(src)

    assert "第一段" in text and "第二段" in text  # 整份轉錄仍然完成
    assert all(refs is None for refs in captured_refs)  # 剪樣本失敗，聲音簿始終是空的


def test_voice_relay_skips_speaker_without_valid_sample_span(monkeypatch, tmp_path):
    """代號沒有帶時間戳的有效發言（例如整段沒有內容）就不會被加進聲音簿。"""
    from app.transcription import media

    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=600, chunk_texts=[
        "講者A：這句沒有時間戳",  # collect_speakers 認得到代號，但無從剪音檔
        "講者A：這句同樣沒有時間戳",
    ])
    cut_calls = []
    monkeypatch.setattr(
        media, "cut_clip",
        lambda *a, **k: cut_calls.append(1) or Path("unused.wav"),
    )
    captured_refs = []

    class Recording(GeminiTranscriber):
        def _transcribe_one(self, audio_path, hint, model=None, on_partial=None,
                            voice_refs=None, user=None):
            captured_refs.append(voice_refs)
            return setup.texts[str(audio_path)]

    Recording(api_key="k", chunk_seconds=240, voice_relay_max_speakers=20, overlap_seconds=0).transcribe(src)

    assert cut_calls == []
    assert all(refs is None for refs in captured_refs)


def test_voice_samples_do_not_overwrite_each_other(monkeypatch, tmp_path):
    """同一個時間戳出現多位講者時，算出來的取樣區間可能完全相同——若用區間
    當檔名，後剪的會蓋掉前一個，聲音簿裡兩個代號指向同一段音訊，等於告訴
    模型「這是A的聲音」又「這是B的聲音」卻放同一段，比沒有樣本更糟。"""
    from app.transcription import media

    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=600, chunk_texts=[
        "[0:05] 講者A：短\n[0:05] 講者B：短\n[0:05] 講者C：短",
        "[0:10] 講者A：第二段",
    ])
    # 來源檔 600 秒（才會分段）；分段檔各 240 秒（夾取範圍的依據）
    monkeypatch.setattr(
        media, "audio_duration",
        lambda p: 240 if "chunk_" in str(p) else 600,
    )
    written = []

    def fake_cut_clip(path, start, end, output_path=None):
        assert output_path is not None, "必須指定輸出路徑，不能讓預設檔名互相覆蓋"
        written.append(Path(output_path))
        return Path(output_path)

    monkeypatch.setattr(media, "cut_clip", fake_cut_clip)
    captured_refs = []

    class Recording(GeminiTranscriber):
        def _transcribe_one(self, audio_path, hint, model=None, on_partial=None,
                            voice_refs=None, user=None):
            captured_refs.append(voice_refs)
            return setup.texts[str(audio_path)]

    Recording(
        api_key="k", chunk_seconds=240, voice_relay_max_speakers=20, overlap_seconds=0
    ).transcribe(src)

    assert len(written) == len(set(written)), f"樣本檔名撞號：{written}"
    paths = [r["path"] for r in captured_refs[1]]
    assert len(paths) == len(set(paths)), "不同代號不可以指向同一個樣本檔"


def test_voice_sample_skipped_when_timestamp_drifts_past_the_chunk(monkeypatch, tmp_path):
    """模型的時間戳會漂（本 repo 實測長音訊會漂到超過實際長度）。漂到分段
    音檔之外時去剪，ffmpeg 會 seek 過頭吐出一段空音訊卻不報錯——那種「樣本」
    上傳上去只會誤導模型，不如不要。"""
    from app.transcription import media

    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=600, chunk_texts=[
        "[5:30] 講者A：這個時間戳已經超出這段音檔的長度",  # chunk 只有 240 秒
        "[0:10] 講者B：這段在範圍內",
    ])
    # 來源檔 600 秒（才會分段）；分段檔各 240 秒（夾取範圍的依據）
    monkeypatch.setattr(
        media, "audio_duration",
        lambda p: 240 if "chunk_" in str(p) else 600,
    )
    cut_spans = []

    def fake_cut_clip(path, start, end, output_path=None):
        cut_spans.append((start, end))
        return Path(output_path or "x.wav")

    monkeypatch.setattr(media, "cut_clip", fake_cut_clip)

    class Recording(GeminiTranscriber):
        def _transcribe_one(self, audio_path, hint, model=None, on_partial=None,
                            voice_refs=None, user=None):
            return setup.texts[str(audio_path)]

    Recording(
        api_key="k", chunk_seconds=240, voice_relay_max_speakers=20, overlap_seconds=0
    ).transcribe(src)

    assert all(start < 240 for start, _ in cut_spans), f"剪到音檔範圍外：{cut_spans}"
    assert all(end <= 240 for _, end in cut_spans), f"結束點超出音檔：{cut_spans}"


def test_flash_fallback_paths_are_gone():
    """強模型備援（失敗段改用 Flash、長檔整份給 Flash）預設本來就關著，
    開了也只是把每日 20 次的 Flash 額度吃掉。整條路徑拿掉，不留沒人用的開關。"""
    import inspect

    params = inspect.signature(GeminiTranscriber.__init__).parameters
    for gone in ("fallback_model", "max_fallback_chunks", "strong_model", "strong_whole_threshold"):
        assert gone not in params


# ---- 一句話被拆成好幾行 ----

def test_prompt_asks_for_whole_sentences_per_line():
    from app.transcription.gemini_transcriber import _TRANSCRIBE_PROMPT

    assert "不要在句子中間換行" in _TRANSCRIBE_PROMPT


def test_fragmented_sentence_comes_back_as_one_line(tmp_path):
    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"RIFF-fake")
    t = GeminiTranscriber(
        api_key="k",
        upload=lambda p: {"handle": str(p)},
        generate=lambda h: "[4:32] 講者B：權限調整本來\n[4:34] 講者B：在上線前的\n[4:35] 講者B：清單裡。",
    )
    assert t.transcribe(wav) == "[4:32] 講者B：權限調整本來在上線前的清單裡。"


def test_fragments_split_across_a_chunk_seam_are_joined(monkeypatch, tmp_path):
    src = tmp_path / "long.wav"
    src.write_bytes(b"RIFF-fake")
    setup = FakeChunkedSetup(monkeypatch, tmp_path, duration=600, chunk_texts=[
        "[3:58] 講者A：我們接下來",
        "[0:01] 講者A：把順序定下來。",
    ])
    text = setup.transcriber(chunk_seconds=240).transcribe(src)
    assert text == "[3:58] 講者A：我們接下來把順序定下來。"
