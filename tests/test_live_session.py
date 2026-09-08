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


# ---- 本次專用詞彙：會前打的詞要進轉錄，不能只進分析 ----

def test_meeting_terms_reach_the_transcriber_from_the_first_chunk(tmp_path):
    """會前打的專案代號要在「聽」的當下就生效。

    只餵給分析的話，逐字稿裡已經是聽錯的字，分析階段再統一寫法也救不回來——
    使用者看到的逐字稿仍然是錯的。
    """
    tr = HintRecordingTranscriber(["講者A：大家好"])
    mgr = LiveSessionManager(tr, tmp_path)
    sid = mgr.start(terms=[{"term": "Kessel 專案", "note": ""}])
    mgr.add_chunk(sid, b"a")
    assert tr.hints[0] and "Kessel 專案" in tr.hints[0]


def test_meeting_terms_and_speaker_hint_coexist(tmp_path):
    """兩種提示是不同面向（用字 vs 講者標籤），第二段起要同時帶上。"""
    tr = HintRecordingTranscriber(["講者A：大家好", "講者B：你好"])
    mgr = LiveSessionManager(tr, tmp_path)
    sid = mgr.start(terms=[{"term": "Kessel 專案", "note": ""}])
    mgr.add_chunk(sid, b"a")
    mgr.add_chunk(sid, b"b")
    assert "講者A" in tr.hints[1] and "Kessel 專案" in tr.hints[1]


def test_terms_are_scoped_to_their_own_session(tmp_path):
    """詞彙跟著 session 走：A 的專案代號不可以混進 B 那場的轉錄提示。"""
    tr = HintRecordingTranscriber(["x", "y"])
    mgr = LiveSessionManager(tr, tmp_path)
    with_terms = mgr.start(terms=[{"term": "Kessel 專案", "note": ""}])
    without = mgr.start()
    mgr.add_chunk(without, b"a")
    assert tr.hints[0] is None
    mgr.add_chunk(with_terms, b"b")
    assert "Kessel 專案" in tr.hints[1]


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


# ---- 閒置 session 的回收 ----
# MediaJobManager 有 _prune_locked 保留最近 100 筆，這裡原本沒有對等機制：
# _sessions 只增不減、使用者關掉分頁沒按結束的 session 音檔也永遠留在磁碟上。

def make_clocked_manager(tmp_path, texts, clock):
    return LiveSessionManager(FakeTranscriber(texts), tmp_path, now=lambda: clock["t"])


def test_idle_session_pruned_after_ttl(tmp_path):
    clock = {"t": 0.0}
    mgr = make_clocked_manager(tmp_path, ["a", "b"], clock)
    abandoned = mgr.start()
    mgr.add_chunk(abandoned, b"x")
    assert (tmp_path / abandoned).exists()

    clock["t"] = LiveSessionManager.SESSION_TTL_SECONDS + 1
    mgr.start()  # 任何一次操作都順手回收過期的

    assert not (tmp_path / abandoned).exists()  # 音檔不再佔磁碟
    with pytest.raises(SessionNotFound):
        mgr.transcript(abandoned)  # 記憶體也放掉了


def test_active_session_survives_long_meeting(tmp_path):
    """兩小時的會議：每段音訊都刷新活躍時間，不能被自己的 TTL 清掉。"""
    clock = {"t": 0.0}
    mgr = make_clocked_manager(tmp_path, ["一", "二", "三"], clock)
    sid = mgr.start()

    for text in ("一", "二", "三"):
        clock["t"] += LiveSessionManager.SESSION_TTL_SECONDS * 0.9
        assert mgr.add_chunk(sid, b"x")["text"] == text

    assert mgr.transcript(sid) == "一\n二\n三"


def test_finished_session_reports_closed_then_is_released(tmp_path):
    """剛結束時遲到的音訊段要拿到「已結束」的明確訊息（不是 404），
    TTL 過後才把整筆放掉。"""
    clock = {"t": 0.0}
    mgr = make_clocked_manager(tmp_path, ["內容"], clock)
    sid = mgr.start()
    mgr.add_chunk(sid, b"a")
    mgr.finish(sid)

    with pytest.raises(ValueError):
        mgr.add_chunk(sid, b"late-chunk")

    clock["t"] += LiveSessionManager.SESSION_TTL_SECONDS + 1
    mgr.start()
    with pytest.raises(SessionNotFound):
        mgr.add_chunk(sid, b"much-later-chunk")


# ---- 預錄聲音辨識：會前每人錄一段樣本，結束時比對出誰是誰 ----

class FakeMatcher:
    def __init__(self, result=None, error=None):
        self.result = result or {}
        self.error = error
        self.calls = []

    def match(self, enrollments, evidence):
        self.calls.append((enrollments, evidence))
        if self.error:
            raise self.error
        return self.result


def _mgr_with_matcher(tmp_path, texts, matcher):
    return LiveSessionManager(
        HintRecordingTranscriber(texts), tmp_path, voice_matcher=matcher
    )


def test_enroll_writes_the_sample_and_counts_people(tmp_path):
    mgr = _mgr_with_matcher(tmp_path, [], FakeMatcher())
    sid = mgr.start()
    assert mgr.enroll(sid, "王小明", b"sample-1") == 1
    assert mgr.enroll(sid, "李美華", b"sample-2") == 2
    files = sorted((tmp_path / sid).glob("enroll_*"))
    assert [f.read_bytes() for f in files] == [b"sample-1", b"sample-2"]


def test_enroll_rejects_more_people_than_the_cap(tmp_path):
    mgr = _mgr_with_matcher(tmp_path, [], FakeMatcher())
    mgr.MAX_ENROLLMENTS = 2
    sid = mgr.start()
    mgr.enroll(sid, "甲", b"a")
    mgr.enroll(sid, "乙", b"b")
    with pytest.raises(ValueError):
        mgr.enroll(sid, "丙", b"c")


def test_enroll_rejects_unsafe_names(tmp_path):
    """姓名會被寫進逐字稿的講者欄，含冒號就會造出假標籤——擋在入口。"""
    mgr = _mgr_with_matcher(tmp_path, [], FakeMatcher())
    sid = mgr.start()
    with pytest.raises(ValueError):
        mgr.enroll(sid, "王小明：主席", b"a")
    with pytest.raises(ValueError):
        mgr.enroll(sid, "   ", b"a")


def test_enroll_belongs_to_its_owner(tmp_path):
    """別人的 session 一律當作不存在（與 add_chunk / finish 同一條規則）。"""
    mgr = _mgr_with_matcher(tmp_path, [], FakeMatcher())
    sid = mgr.start(user="alice")
    with pytest.raises(SessionNotFound):
        mgr.enroll(sid, "王小明", b"a", user="bob")


def test_voice_mapping_without_enrollments_never_calls_the_matcher(tmp_path):
    """沒開這個功能就不能有任何成本——連 API 都不該打。"""
    matcher = FakeMatcher({"講者A": "王小明"})
    mgr = _mgr_with_matcher(tmp_path, ["[0:01] 講者A：大家好"], matcher)
    sid = mgr.start()
    mgr.add_chunk(sid, b"audio")
    assert mgr.voice_mapping(sid) == {}
    assert matcher.calls == []


def test_voice_mapping_passes_samples_and_labelled_evidence(tmp_path):
    """比對要同時拿到樣本音檔，以及「音訊＋那段的逐字稿」的證據。"""
    matcher = FakeMatcher({"講者A": "王小明"})
    mgr = _mgr_with_matcher(tmp_path, ["[0:01] 講者A：大家好"], matcher)
    sid = mgr.start()
    mgr.enroll(sid, "王小明", b"sample")
    mgr.add_chunk(sid, b"audio", offset_seconds=0)  # 前端一律帶偏移，時間戳才留得住

    assert mgr.voice_mapping(sid) == {"講者A": "王小明"}
    enrollments, evidence = matcher.calls[0]
    assert [e["name"] for e in enrollments] == ["王小明"]
    assert enrollments[0]["path"].read_bytes() == b"sample"
    assert evidence[0]["transcript"] == "[0:01] 講者A：大家好"
    assert evidence[0]["path"].read_bytes() == b"audio"


def test_voice_mapping_survives_a_matcher_failure(tmp_path):
    """比對是加分項，掛掉就回空對應，不能擋住一場已經開完的會議。"""
    matcher = FakeMatcher(error=RuntimeError("額度用完"))
    mgr = _mgr_with_matcher(tmp_path, ["[0:01] 講者A：大家好"], matcher)
    sid = mgr.start()
    mgr.enroll(sid, "王小明", b"sample")
    mgr.add_chunk(sid, b"audio")
    assert mgr.voice_mapping(sid) == {}


def test_samples_are_deleted_with_the_session(tmp_path):
    """聲紋是生物特徵資料：跟著錄音段一起在 finish() 就刪掉，不留在磁碟。"""
    mgr = _mgr_with_matcher(tmp_path, ["[0:01] 講者A：大家好"], FakeMatcher())
    sid = mgr.start()
    mgr.enroll(sid, "王小明", b"sample")
    mgr.add_chunk(sid, b"audio")
    assert list((tmp_path / sid).glob("enroll_*"))
    mgr.finish(sid)
    assert not (tmp_path / sid).exists()


def test_enroll_rejects_a_duplicate_name(tmp_path):
    """兩份樣本掛同一個名字，比對只會更混亂——而且下游遇到重複姓名會整批放棄。

    使用者多半是分不清哪一列還沒錄而重錄了同一個人，當場擋下來比事後失效好。
    """
    mgr = _mgr_with_matcher(tmp_path, [], FakeMatcher())
    sid = mgr.start()
    mgr.enroll(sid, "王小明", b"a")
    with pytest.raises(ValueError):
        mgr.enroll(sid, "王小明", b"b")
    with pytest.raises(ValueError):
        mgr.enroll(sid, "  王小明  ", b"b")  # 前後空白不算不同的人
