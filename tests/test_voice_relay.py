"""聲紋跨段接力（approach A）的測試。

分段轉錄時，替每個講者代號從「他首次清楚發言」的那一段剪一小截聲音，
接力餵給後續分段，讓模型靠嗓音沿用同一代號、不再每段重新編號。

這一支先測純函式 speaker_sample_span：在一段（本地時間戳的）逐字稿裡，
替某個代號挑一段有代表性的發言時間區間，供上游剪音檔。
"""
from pathlib import Path
from types import SimpleNamespace

from app.transcription.gemini_transcriber import voice_relay_parts
from app.transcription.segments import speaker_sample_span


def test_sample_span_picks_longest_utterance_of_the_code():
    text = "\n".join([
        "[0:00] 講者A：開會。",
        "[0:05] 講者B：好的我這邊補充一下這個案子的背景跟後續。",
        "[0:20] 講者A：我想請問這個預算的細節到底是怎麼估算出來的呢。",
        "[0:35] 講者B：好。",
    ])
    span = speaker_sample_span(text, "講者A", max_seconds=8)
    assert span is not None
    start, end = span
    assert start == 20.0          # 較長那句在 0:20，勝過 0:00 的「開會」
    assert end == 28.0            # 下一個時間戳 0:35 vs start+8=28 → 取小


def test_sample_span_end_bounded_by_next_timestamp():
    text = "[1:00] 講者A：這句話夠長可以當樣本。\n[1:03] 講者B：換人。"
    start, end = speaker_sample_span(text, "講者A", max_seconds=8, min_seconds=2)
    assert start == 60.0
    assert end == 63.0            # 下一個時間戳 1:03 比 start+8 近


def test_sample_span_extends_to_min_when_next_ts_too_close():
    text = "[1:00] 講者A：短短一句。\n[1:01] 講者B：換。"
    start, end = speaker_sample_span(text, "講者A", max_seconds=8, min_seconds=2)
    assert start == 60.0
    assert end == 62.0            # 下一個時間戳只差 1 秒，撐到 min_seconds


def test_sample_span_last_utterance_uses_max_seconds():
    text = "[0:00] 講者B：開始。\n[2:00] 講者A：最後一段沒有下一個時間戳可界定。"
    start, end = speaker_sample_span(text, "講者A", max_seconds=8)
    assert start == 120.0
    assert end == 128.0           # 沒有下一個時間戳 → start + max


def test_sample_span_none_when_code_absent():
    assert speaker_sample_span("[0:00] 講者A：hi", "講者C") is None


def test_sample_span_none_when_code_has_no_timestamp():
    # 沒有時間戳就無從在音檔裡定位，不能拿來剪樣本
    assert speaker_sample_span("講者A：這句沒有時間戳", "講者A") is None


def test_sample_span_normalizes_code_spacing_and_case():
    text = "[0:10] 講者 a：這裡用了空格跟小寫。"
    span = speaker_sample_span(text, "講者A")
    assert span is not None and span[0] == 10.0


# ---- voice_relay_parts：把聲音簿組成 Gemini contents 的一段前導內容 ----


def test_voice_relay_parts_empty_when_no_refs():
    assert voice_relay_parts([]) == []
    assert voice_relay_parts(None) == []


def test_voice_relay_parts_includes_label_and_path_for_each_ref():
    refs = [
        {"label": "講者A", "path": Path("a.wav")},
        {"label": "講者B", "path": Path("b.wav")},
    ]
    parts = voice_relay_parts(refs)
    assert Path("a.wav") in parts
    assert Path("b.wav") in parts
    # 每個樣本前面要有講者標示，模型才知道哪段音訊對應哪個代號
    joined = "\n".join(p for p in parts if isinstance(p, str))
    assert "講者A" in joined and "講者B" in joined


def test_voice_relay_parts_instructs_reuse_not_blind_relabel():
    parts = voice_relay_parts([{"label": "講者A", "path": Path("a.wav")}])
    joined = "\n".join(p for p in parts if isinstance(p, str))
    assert "沿用" in joined
    assert "新的代號" in joined or "新代號" in joined


def test_voice_relay_parts_refs_appear_before_usage_instruction():
    """先給樣本音訊，指示文字放最後——模型讀完全部樣本才看到「請沿用」的
    要求，不會在還沒看到樣本前就被要求做判斷。"""
    parts = voice_relay_parts([{"label": "講者A", "path": Path("a.wav")}])
    audio_index = parts.index(Path("a.wav"))
    instruction_index = next(
        i for i, p in enumerate(parts) if isinstance(p, str) and "沿用" in p
    )
    assert audio_index < instruction_index


# ---- 樣本上傳失敗的處理：聲紋是加分項，壞掉不可以拖垮整段轉錄 ----
#
# 這幾個測試需要看到 _transcribe_with_key 內部真正的上傳／清理流程，
# 所以自備一個最小的假 genai client（其餘測試都用更淺的 upload/generate 注入）。


class FakeHandle:
    def __init__(self, name, state="ACTIVE"):
        self.name = name
        self.state = SimpleNamespace(name=state)


class FakeFiles:
    """記錄上傳與刪除，可指定某個檔案上傳時炸掉或處理後變成 FAILED。"""

    def __init__(self, raise_on=None, failed_state_on=None):
        self.raise_on = raise_on
        self.failed_state_on = failed_state_on
        self.uploaded = []
        self.deleted = []
        self._n = 0

    def upload(self, file):
        name = str(file)
        if self.raise_on and self.raise_on in name:
            raise RuntimeError("上傳炸了")
        self._n += 1
        state = "FAILED" if (self.failed_state_on and self.failed_state_on in name) else "ACTIVE"
        handle = FakeHandle(f"files/{self._n}", state)
        self.uploaded.append(name)
        return handle

    def get(self, name):
        return FakeHandle(name)

    def delete(self, name):
        self.deleted.append(name)


class FakeClient:
    def __init__(self, files):
        self.files = files
        self.models = SimpleNamespace(
            generate_content=lambda **kw: SimpleNamespace(text="[0:00] 講者A：轉出來了")
        )


def _transcriber_with_fake_client(files, tmp_path):
    from app.transcription.gemini_transcriber import GeminiTranscriber

    t = GeminiTranscriber(api_key="k", voice_relay_max_speakers=20)
    t._client = lambda key: FakeClient(files)
    return t


def test_voice_ref_upload_failure_does_not_kill_the_transcription(tmp_path):
    """樣本上傳失敗只該讓這次沒有參考音訊，不該讓整段轉錄失敗——
    聲紋接力是加分項，壞掉時要退回既有的純文字提示行為。"""
    main = tmp_path / "chunk_000.wav"
    main.write_bytes(b"RIFF")
    ref = tmp_path / "sample.wav"
    ref.write_bytes(b"RIFF")

    files = FakeFiles(raise_on="sample.wav")
    t = _transcriber_with_fake_client(files, tmp_path)

    text = t._transcribe_with_key(
        "k", main, voice_refs=[{"label": "講者A", "path": ref}]
    )
    assert "轉出來了" in text


def test_voice_ref_processing_failure_does_not_kill_the_transcription(tmp_path):
    """樣本上傳後被 Gemini 判定 FAILED 也一樣：轉錄要繼續。"""
    main = tmp_path / "chunk_000.wav"
    main.write_bytes(b"RIFF")
    ref = tmp_path / "sample.wav"
    ref.write_bytes(b"RIFF")

    files = FakeFiles(failed_state_on="sample.wav")
    t = _transcriber_with_fake_client(files, tmp_path)

    text = t._transcribe_with_key(
        "k", main, voice_refs=[{"label": "講者A", "path": ref}]
    )
    assert "轉出來了" in text


def test_uploaded_voice_refs_are_deleted_when_a_later_upload_fails(tmp_path):
    """中途炸掉時，已經上傳成功的樣本也要刪掉——Files API 有儲存上限，
    漏刪的檔案會一直累積到撞上限。"""
    main = tmp_path / "chunk_000.wav"
    main.write_bytes(b"RIFF")
    ok = tmp_path / "good.wav"
    ok.write_bytes(b"RIFF")
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"RIFF")

    files = FakeFiles(raise_on="bad.wav")
    t = _transcriber_with_fake_client(files, tmp_path)

    t._transcribe_with_key(
        "k", main,
        voice_refs=[{"label": "講者A", "path": ok}, {"label": "講者B", "path": bad}],
    )
    # good.wav 上傳成功過，就必須被刪掉，不能因為後面炸了而漏掉
    assert len(files.deleted) == len(files.uploaded)
