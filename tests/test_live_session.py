"""即時聆聽 session 管理測試。"""
import threading

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


# ---- 並發：段落順序＝錄音順序，而非「哪段先辨識完」 ----

class HintRecordingTranscriber:
    def __init__(self, texts):
        self.texts = list(texts)
        self.hints = []

    def transcribe(self, path, on_progress=None, hint=None):
        self.hints.append(hint)
        return self.texts.pop(0)


def test_known_speakers_passed_as_hint_to_next_chunk(tmp_path):
    """跨段講者一致性：第二段轉錄要帶入前面已出現的講者，模型才能沿用標籤。"""
    tr = HintRecordingTranscriber(["講者A：大家好", "講者B：你好"])
    mgr = LiveSessionManager(tr, tmp_path)
    sid = mgr.start()
    mgr.add_chunk(sid, b"a")
    mgr.add_chunk(sid, b"b")
    assert tr.hints[0] is None  # 第一段沒有先前講者
    assert tr.hints[1] and "講者A" in tr.hints[1]  # 第二段帶入已知講者


class GatedTranscriber:
    """轉錄結果＝檔案內容；每段先等對應的閘門開啟，好在測試裡控制完成順序。"""

    def __init__(self):
        self.gates = {}

    def transcribe(self, path, on_progress=None, hint=None):
        from pathlib import Path

        text = Path(path).read_bytes().decode()
        self.gates.setdefault(text, threading.Event()).wait(2)
        return text


def test_chunk_order_follows_submission_not_completion(tmp_path):
    tr = GatedTranscriber()
    mgr = LiveSessionManager(tr, tmp_path)
    sid = mgr.start()
    session_dir = tmp_path / sid

    def add(data):
        mgr.add_chunk(sid, data)

    t1 = threading.Thread(target=add, args=(b"first",))
    t1.start()
    _wait_for(session_dir / "chunk_000.webm")  # first 已配到 index 0
    t2 = threading.Thread(target=add, args=(b"second",))
    t2.start()
    _wait_for(session_dir / "chunk_001.webm")  # second 已配到 index 1

    # 讓第二段先辨識完，第一段後完成
    tr.gates.setdefault("second", threading.Event()).set()
    tr.gates.setdefault("first", threading.Event()).set()
    t1.join(2)
    t2.join(2)

    # 完成順序是 second→first，但最終逐字稿仍照錄音順序
    assert mgr.transcript(sid) == "first\nsecond"


def _wait_for(path, timeout=2.0):
    import time

    deadline = time.time() + timeout
    while not path.exists() and time.time() < deadline:
        time.sleep(0.01)


# ---- 時間戳平移：chunk 內相對時間 → 整場會議時間 ----

def test_chunk_timestamps_shifted_by_offset(tmp_path):
    """每段獨立轉錄時模型標的是段內相對時間，要加上段落開始秒數。"""
    mgr = LiveSessionManager(
        FakeTranscriber(["[0:03] 講者A：開始討論\n[0:41] 講者B：我補充一下"]),
        tmp_path,
    )
    sid = mgr.start()
    r = mgr.add_chunk(sid, b"a", offset_seconds=45)
    assert "[0:48] 講者A：開始討論" in r["text"]
    assert "[1:26] 講者B：我補充一下" in r["text"]
    # 講者掃描不受時間前綴影響
    assert mgr._sessions[sid].speakers == ["講者A", "講者B"]


def test_chunk_without_offset_strips_relative_timestamps(tmp_path):
    """舊前端沒傳 offset：段內相對時間是錯的，寧可剝掉也不誤導。"""
    mgr = LiveSessionManager(FakeTranscriber(["[0:03] 講者A：哈囉"]), tmp_path)
    sid = mgr.start()
    r = mgr.add_chunk(sid, b"a")
    assert r["text"] == "講者A：哈囉"


def test_chunk_without_markers_gets_offset_prefix(tmp_path):
    """轉錄後端沒標時間（如本地 Whisper）：段首補開始時間，保住段落級時間軸。"""
    mgr = LiveSessionManager(FakeTranscriber(["講者A：哈囉"]), tmp_path)
    sid = mgr.start()
    r = mgr.add_chunk(sid, b"a", offset_seconds=90)
    assert r["text"] == "[1:30] 講者A：哈囉"


def test_offset_over_an_hour_uses_hms(tmp_path):
    mgr = LiveSessionManager(FakeTranscriber(["[0:10] 講者A：收尾"]), tmp_path)
    sid = mgr.start()
    r = mgr.add_chunk(sid, b"a", offset_seconds=3600)
    assert r["text"].startswith("[1:00:10]")


def test_translation_receives_text_without_timestamps(tmp_path):
    captured = {}

    class SpyTranslator:
        def translate(self, text, target):
            captured["text"] = text
            return "hello"

    mgr = LiveSessionManager(
        FakeTranscriber(["[0:03] 講者A：哈囉"]), tmp_path, translator=SpyTranslator()
    )
    sid = mgr.start(translate_to="en")
    r = mgr.add_chunk(sid, b"a", offset_seconds=0)
    assert captured["text"] == "講者A：哈囉"
    assert r["translation"] == "hello"
