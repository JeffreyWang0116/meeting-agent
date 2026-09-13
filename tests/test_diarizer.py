"""Diarizer：轉錄工作與 pyannote 之間的接合層。

它要做到兩件事：一是與 Gemini 轉錄**並行**（上傳＋分群不必排在轉錄後面），
二是**任何失敗都不能讓轉錄失敗**——講者辨識是加分項，出事就沿用 Gemini 的代號。
"""
from concurrent.futures import Future
from pathlib import Path

import pytest

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
        self.voiceprinted: list[Path] = []
        self.identified: list[tuple] = []
        self.voiceprint_errors: dict[str, Exception] = {}

    def diarize(self, path):
        self.diarized.append(Path(path))
        if self.error:
            raise self.error
        return self.output

    def voiceprint(self, path):
        self.voiceprinted.append(Path(path))
        for marker, error in self.voiceprint_errors.items():
            if marker in Path(path).name:
                raise error
        return f"vp-of-{Path(path).stem}"

    def identify(self, path, voiceprints, threshold=None):
        self.identified.append((Path(path), dict(voiceprints), threshold))
        if self.error:
            raise self.error
        return self.output


def fake_encode(tmp_path, calls=None):
    def encode(src, max_seconds=None):
        if calls is not None:
            calls.append((Path(src), max_seconds))
        name = "encoded.ogg" if max_seconds is None else f"{Path(src).stem}_vp.ogg"
        out = tmp_path / name
        out.write_bytes(b"ogg")
        return out
    return encode


def make(tmp_path, client=None, **kwargs):
    kwargs.setdefault("encode", fake_encode(tmp_path))
    kwargs.setdefault("duration", lambda path: 10.0)
    # 既有的 identify 測試驗的是「開啟 voiceprint」那條路；預設關閉另有專門的測試
    kwargs.setdefault("voiceprints_enabled", True)
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


# ---- 即時聆聽：整場串接後重標 ----

def fake_concat(durations, calls):
    def concat(pieces, dest):
        calls.append((list(pieces), Path(dest)))
        Path(dest).write_bytes(b"ogg")
        return durations
    return concat


def live_pieces(tmp_path):
    # 第二段前端回報從 45 秒開始、與前一段重疊 3 秒 → 串接時略過開頭 3 秒，
    # 保留下來的部分從整場 48 秒開始
    return [
        {"path": tmp_path / "chunk_000.webm", "skip": 0.0, "start": 0.0},
        {"path": tmp_path / "chunk_001.webm", "skip": 3.0, "start": 48.0},
    ]


LIVE_TRANSCRIPT = "[0:10] 講者A：第一段\n[0:53] 講者A：第二段"
# 串接檔時間：SPEAKER_01 在第一段 5~15 秒；SPEAKER_00 在串接檔 50~55 秒，
# 落在第二段（串接檔 45 秒起）的第 5~10 秒＝整場 53~58 秒
LIVE_OUTPUT = {
    "exclusiveDiarization": [
        {"speaker": "SPEAKER_01", "start": 5, "end": 15},
        {"speaker": "SPEAKER_00", "start": 50, "end": 55},
    ],
    "voiceprints": [
        {"speaker": "SPEAKER_00", "match": "王小明", "confidence": {"王小明": 89}},
        {"speaker": "SPEAKER_01", "match": None, "confidence": {"王小明": 20}},
    ],
}


def test_relabel_session_concats_chunks_skipping_overlap_then_diarizes(tmp_path):
    concat_calls = []
    client = FakeClient(output=LIVE_OUTPUT)
    diarizer = make(tmp_path, client, concat=fake_concat([45.0, 42.0], concat_calls))

    text, prior = diarizer.relabel_session(LIVE_TRANSCRIPT, live_pieces(tmp_path), [])

    (pieces, dest), = concat_calls
    assert pieces == [(tmp_path / "chunk_000.webm", 0.0), (tmp_path / "chunk_001.webm", 3.0)]
    assert client.diarized == [dest]
    # 沒有預錄樣本：不比對聲紋，也不建任何 voiceprint（按個計費）
    assert client.identified == [] and client.voiceprinted == []
    assert text == "[0:10] 講者A：第一段\n[0:53] 講者B：第二段"
    assert prior == {}


def test_relabel_session_with_enrollments_identifies_and_returns_names(tmp_path):
    encode_calls = []
    client = FakeClient(output=LIVE_OUTPUT)
    diarizer = make(
        tmp_path, client,
        encode=fake_encode(tmp_path, encode_calls),
        concat=fake_concat([45.0, 42.0], []),
        voiceprint_threshold=50,
    )
    enrollments = [{"name": "王小明", "path": tmp_path / "enroll_00.webm"}]

    text, prior = diarizer.relabel_session(LIVE_TRANSCRIPT, live_pieces(tmp_path), enrollments)

    assert encode_calls == [(tmp_path / "enroll_00.webm", 30)]  # voiceprint 樣本上限 30 秒
    assert client.voiceprinted == [tmp_path / "enroll_00_vp.ogg"]
    (_, voiceprints, threshold), = client.identified
    assert voiceprints == {"王小明": "vp-of-enroll_00_vp"}
    assert threshold == 50
    assert client.diarized == []
    assert prior == {"講者B": "王小明"}
    assert text.endswith("講者B：第二段")


def test_relabel_session_skips_a_person_whose_voiceprint_fails(tmp_path):
    client = FakeClient(output=LIVE_OUTPUT)
    client.voiceprint_errors = {"enroll_01": PyannoteError("too short")}
    diarizer = make(tmp_path, client, concat=fake_concat([45.0, 42.0], []))
    enrollments = [
        {"name": "王小明", "path": tmp_path / "enroll_00.webm"},
        {"name": "李大華", "path": tmp_path / "enroll_01.webm"},
    ]
    diarizer.relabel_session(LIVE_TRANSCRIPT, live_pieces(tmp_path), enrollments)
    (_, voiceprints, _), = client.identified
    assert list(voiceprints) == ["王小明"]


def test_relabel_session_falls_back_to_plain_diarize_when_no_voiceprint_survives(tmp_path):
    client = FakeClient(output=LIVE_OUTPUT)
    client.voiceprint_errors = {"enroll": PyannoteError("HTTP 402")}
    diarizer = make(tmp_path, client, concat=fake_concat([45.0, 42.0], []))
    _, prior = diarizer.relabel_session(
        LIVE_TRANSCRIPT, live_pieces(tmp_path),
        [{"name": "王小明", "path": tmp_path / "enroll_00.webm"}],
    )
    assert client.identified == [] and len(client.diarized) == 1
    assert prior == {}


def test_relabel_session_reports_every_pyannote_job(tmp_path):
    calls = []
    client = FakeClient(output=LIVE_OUTPUT)
    diarizer = make(
        tmp_path, client, on_call=lambda: calls.append(1),
        concat=fake_concat([45.0, 42.0], []),
    )
    enrollments = [
        {"name": "王小明", "path": tmp_path / "enroll_00.webm"},
        {"name": "李大華", "path": tmp_path / "enroll_01.webm"},
    ]
    diarizer.relabel_session(LIVE_TRANSCRIPT, live_pieces(tmp_path), enrollments)
    assert len(calls) == 3  # 兩個 voiceprint＋一次 identify


def test_relabel_session_deletes_its_temporary_audio(tmp_path):
    client = FakeClient(output=LIVE_OUTPUT)
    diarizer = make(tmp_path, client, concat=fake_concat([45.0, 42.0], []))
    diarizer.relabel_session(
        LIVE_TRANSCRIPT, live_pieces(tmp_path),
        [{"name": "王小明", "path": tmp_path / "enroll_00.webm"}],
    )
    assert list(tmp_path.rglob("*.ogg")) == []


def test_relabel_session_raises_so_the_caller_can_fall_back(tmp_path):
    client = FakeClient(error=PyannoteError("HTTP 402"))
    diarizer = make(tmp_path, client, concat=fake_concat([45.0, 42.0], []))
    with pytest.raises(PyannoteError):
        diarizer.relabel_session(LIVE_TRANSCRIPT, live_pieces(tmp_path), [])
    assert list(tmp_path.rglob("*.ogg")) == []


def test_build_diarizer_passes_voiceprint_threshold():
    diarizer = build_diarizer(Settings(
        pyannote_api_key="k", transcribe_engine="gemini", voiceprint_match_threshold=65,
    ))
    assert diarizer.voiceprint_threshold == 65


def test_relabel_session_never_creates_voiceprints_when_disabled(tmp_path):
    client = FakeClient(output=LIVE_OUTPUT)
    diarizer = make(
        tmp_path, client, concat=fake_concat([45.0, 42.0], []), voiceprints_enabled=False,
    )
    _, prior = diarizer.relabel_session(
        LIVE_TRANSCRIPT, live_pieces(tmp_path),
        [{"name": "王小明", "path": tmp_path / "enroll_00.webm"}],
    )
    assert client.voiceprinted == [] and client.identified == []
    assert prior == {}


def test_voiceprints_are_disabled_by_default():
    assert Diarizer(FakeClient()).voiceprints_enabled is False


def test_build_diarizer_passes_voiceprint_switch():
    on = build_diarizer(Settings(
        pyannote_api_key="k", transcribe_engine="gemini", pyannote_voiceprint_enabled=True,
    ))
    off = build_diarizer(Settings(pyannote_api_key="k", transcribe_engine="gemini"))
    assert on.voiceprints_enabled is True
    assert off.voiceprints_enabled is False
