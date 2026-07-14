"""即時聆聽 session 管理測試。"""
import pytest

from app.transcription.live_session import LiveSessionManager, SessionNotFound


class FakeTranscriber:
    """依序回傳預先設定的文字，並記錄收到的檔案。"""

    def __init__(self, texts):
        self.texts = list(texts)
        self.received = []

    def transcribe(self, path, on_progress=None):
        self.received.append(path)
        return self.texts.pop(0)


@pytest.fixture
def manager(tmp_path):
    def make(texts):
        return LiveSessionManager(FakeTranscriber(texts), tmp_path)

    return make


def test_chunks_accumulate_transcript(manager):
    mgr = manager(["大家好", "今天討論 demo 的分工"])
    sid = mgr.start()

    r1 = mgr.add_chunk(sid, b"fake-audio-1")
    assert r1["text"] == "大家好"
    assert r1["transcript"] == "大家好"

    r2 = mgr.add_chunk(sid, b"fake-audio-2")
    assert r2["transcript"] == "大家好\n今天討論 demo 的分工"


def test_chunk_files_written_to_session_dir(manager, tmp_path):
    mgr = manager(["x"])
    sid = mgr.start()
    mgr.add_chunk(sid, b"\x1a\x45\xdf\xa3", suffix=".webm")

    session_dir = tmp_path / sid
    files = list(session_dir.glob("chunk_*.webm"))
    assert len(files) == 1
    assert files[0].read_bytes() == b"\x1a\x45\xdf\xa3"


def test_empty_transcription_not_appended(manager):
    mgr = manager(["", "有話了"])
    sid = mgr.start()
    assert mgr.add_chunk(sid, b"silence")["transcript"] == ""
    assert mgr.add_chunk(sid, b"speech")["transcript"] == "有話了"


def test_finish_closes_session(manager):
    mgr = manager(["內容"])
    sid = mgr.start()
    mgr.add_chunk(sid, b"a")

    assert mgr.finish(sid) == "內容"
    with pytest.raises(ValueError):
        mgr.add_chunk(sid, b"late-chunk")


def test_unknown_session_raises(manager):
    mgr = manager([])
    with pytest.raises(SessionNotFound):
        mgr.add_chunk("nope", b"a")
    with pytest.raises(SessionNotFound):
        mgr.finish("nope")


class FakeTranslator:
    def translate(self, text, target):
        return f"[{target}] {text}"


def test_chunk_translated_when_session_requests_it(tmp_path):
    mgr = LiveSessionManager(
        FakeTranscriber(["大家好"]), tmp_path, translator=FakeTranslator()
    )
    sid = mgr.start(translate_to="en")
    r = mgr.add_chunk(sid, b"a")
    assert r["text"] == "大家好"
    assert r["translation"] == "[en] 大家好"


def test_chunk_not_translated_by_default(tmp_path):
    mgr = LiveSessionManager(
        FakeTranscriber(["大家好"]), tmp_path, translator=FakeTranslator()
    )
    sid = mgr.start()
    assert mgr.add_chunk(sid, b"a")["translation"] is None


def test_translation_failure_does_not_break_transcription(tmp_path):
    class BrokenTranslator:
        def translate(self, text, target):
            raise RuntimeError("quota")

    mgr = LiveSessionManager(
        FakeTranscriber(["大家好"]), tmp_path, translator=BrokenTranslator()
    )
    sid = mgr.start(translate_to="en")
    r = mgr.add_chunk(sid, b"a")
    # 翻譯壞了逐字稿仍要照常運作
    assert r["text"] == "大家好"
    assert r["translation"] is None


def test_sessions_are_independent(manager, tmp_path):
    mgr = LiveSessionManager(FakeTranscriber(["s1 的話", "s2 的話"]), tmp_path)
    sid1, sid2 = mgr.start(), mgr.start()
    mgr.add_chunk(sid1, b"a")
    mgr.add_chunk(sid2, b"b")
    assert mgr.transcript(sid1) == "s1 的話"
    assert mgr.transcript(sid2) == "s2 的話"
