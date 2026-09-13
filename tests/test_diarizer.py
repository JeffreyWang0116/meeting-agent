"""Diarizer：轉錄工作與 pyannote 之間的接合層。

它要做到兩件事：一是與 Gemini 轉錄**並行**（上傳＋分群不必排在轉錄後面），
二是**任何失敗都不能讓轉錄失敗**——講者辨識是加分項，出事就沿用 Gemini 的代號。
"""
from concurrent.futures import Future
from pathlib import Path

from app.config import Settings
from app.transcription.diarizer import Diarizer, build_diarizer
from app.transcription.pyannote_client import PyannoteError


def run_now(fn):
    """同步版 submit：測試不必開執行緒。"""
    future = Future()
    try:
        future.set_result(fn())
    except Exception as exc:
        future.set_exception(exc)
    return future


class FakeClient:
    def __init__(self, output=None, error=None):
        self.output = output if output is not None else {
            "exclusiveDiarization": [
                {"speaker": "SPEAKER_00", "start": 0, "end": 4.5},
                {"speaker": "SPEAKER_01", "start": 5, "end": 9},
            ]
        }
        self.error = error
        self.diarized: list[Path] = []

    def diarize(self, path):
        self.diarized.append(Path(path))
        if self.error:
            raise self.error
        return self.output


def fake_encode(tmp_path):
    def encode(src):
        out = tmp_path / "encoded.ogg"
        out.write_bytes(b"ogg")
        return out
    return encode


def make(tmp_path, client=None, **kwargs):
    kwargs.setdefault("encode", fake_encode(tmp_path))
    kwargs.setdefault("duration", lambda path: 10.0)
    return Diarizer(client or FakeClient(), submit=run_now, **kwargs)


TRANSCRIPT = "[0:00] 講者A：主席請\n[0:05] 講者A：謝謝主席"


def test_start_encodes_then_diarizes_the_encoded_file(tmp_path):
    client = FakeClient()
    diarizer = make(tmp_path, client)
    diarizer.start(tmp_path / "meeting.wav").result()
    assert client.diarized == [tmp_path / "encoded.ogg"]


def test_encoded_upload_copy_is_deleted_afterwards(tmp_path):
    diarizer = make(tmp_path)
    diarizer.start(tmp_path / "meeting.wav").result()
    assert not (tmp_path / "encoded.ogg").exists()


def test_encoded_copy_is_deleted_even_when_diarize_fails(tmp_path):
    diarizer = make(tmp_path, FakeClient(error=PyannoteError("boom")))
    diarizer.start(tmp_path / "meeting.wav")
    assert not (tmp_path / "encoded.ogg").exists()


def test_apply_relabels_transcript_with_diarized_speakers(tmp_path):
    diarizer = make(tmp_path)
    future = diarizer.start(tmp_path / "meeting.wav")
    assert diarizer.apply(future, TRANSCRIPT) == "[0:00] 講者A：主席請\n[0:05] 講者B：謝謝主席"


def test_apply_uses_source_duration_to_drop_hallucinated_lines(tmp_path):
    diarizer = make(tmp_path, duration=lambda path: 10.0)
    future = diarizer.start(tmp_path / "meeting.wav")
    text = diarizer.apply(future, TRANSCRIPT + "\n[5:24:40] 講者A：休息十分鐘")
    assert "休息十分鐘" not in text


def test_apply_falls_back_to_plain_diarization_when_exclusive_missing(tmp_path):
    output = {"diarization": [{"speaker": "SPEAKER_07", "start": 0, "end": 9}]}
    diarizer = make(tmp_path, FakeClient(output=output))
    future = diarizer.start(tmp_path / "meeting.wav")
    assert diarizer.apply(future, TRANSCRIPT) == "[0:00] 講者A：主席請\n[0:05] 講者A：謝謝主席"


def test_apply_keeps_gemini_labels_when_pyannote_fails(tmp_path):
    diarizer = make(tmp_path, FakeClient(error=PyannoteError("HTTP 402")))
    future = diarizer.start(tmp_path / "meeting.wav")
    assert diarizer.apply(future, TRANSCRIPT) == TRANSCRIPT


def test_apply_keeps_gemini_labels_when_encoding_fails(tmp_path):
    def broken(src):
        raise RuntimeError("ffmpeg missing")
    diarizer = make(tmp_path, encode=broken)
    future = diarizer.start(tmp_path / "meeting.wav")
    assert diarizer.apply(future, TRANSCRIPT) == TRANSCRIPT


def test_apply_keeps_gemini_labels_when_nothing_was_detected(tmp_path):
    diarizer = make(tmp_path, FakeClient(output={"exclusiveDiarization": []}))
    future = diarizer.start(tmp_path / "meeting.wav")
    assert diarizer.apply(future, TRANSCRIPT) == TRANSCRIPT


def test_each_pyannote_job_is_reported_for_usage_stats(tmp_path):
    calls = []
    diarizer = make(tmp_path, on_call=lambda: calls.append(1))
    diarizer.start(tmp_path / "meeting.wav").result()
    assert calls == [1]


def test_unknown_duration_never_drops_lines(tmp_path):
    diarizer = make(tmp_path, duration=lambda path: None)
    future = diarizer.start(tmp_path / "meeting.wav")
    assert "休息" in diarizer.apply(future, TRANSCRIPT + "\n[5:24:40] 講者A：休息")


# ---- 建不建 ----

def test_build_diarizer_needs_key_and_gemini_engine():
    assert build_diarizer(Settings(pyannote_api_key=None, transcribe_engine="gemini")) is None
    # 本地 Whisper 輸出沒有時間戳，對齊不起來，送去分群只是白花錢
    assert build_diarizer(Settings(pyannote_api_key="k", transcribe_engine="local")) is None
    assert isinstance(
        build_diarizer(Settings(pyannote_api_key="k", transcribe_engine="gemini")), Diarizer
    )


def test_build_diarizer_passes_model_and_timeout():
    diarizer = build_diarizer(Settings(
        pyannote_api_key="k", transcribe_engine="gemini",
        pyannote_model="community-1", diarize_timeout_seconds=120,
    ))
    assert diarizer.client.model == "community-1"
    assert diarizer.client.timeout_seconds == 120
