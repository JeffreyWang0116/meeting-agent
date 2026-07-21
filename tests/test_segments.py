"""分段逐字稿縫合工具測試。

即時聆聽與長音檔上傳都靠這組函式把「各自獨立轉錄的段落」接回一份
連續的逐字稿——時間戳要平移回整場時間，講者標籤要跨段沿用同一組。
"""
from app.transcription.segments import (
    collect_speakers,
    format_time,
    parse_time_label,
    shift_timestamps,
    speaker_of,
    speaker_hint,
)


# ---- 時間格式 ----

def test_format_time_under_an_hour():
    assert format_time(0) == "0:00"
    assert format_time(62) == "1:02"
    assert format_time(3599) == "59:59"


def test_format_time_over_an_hour():
    assert format_time(3600) == "1:00:00"
    assert format_time(3610) == "1:00:10"


def test_format_time_never_negative():
    assert format_time(-5) == "0:00"


def test_parse_time_label_round_trip():
    for seconds in (0, 62, 3599, 3610):
        assert parse_time_label(format_time(seconds)) == seconds


# ---- 講者擷取 ----

def test_speaker_of_skips_time_prefix():
    """時間戳裡的冒號不能被誤認成講者標籤的分隔符。"""
    assert speaker_of("[0:05] 講者A：你好") == "講者A"
    assert speaker_of("[1:02:03] Kevin: hello") == "Kevin"


def test_speaker_of_without_time_prefix():
    assert speaker_of("講者B：換我說") == "講者B"


def test_speaker_of_returns_none_when_unlabelled():
    assert speaker_of("[0:05] 這句沒有講者標籤") is None
    assert speaker_of("") is None


def test_collect_speakers_preserves_first_appearance_order():
    known = []
    collect_speakers("[0:00] 講者B：先講\n[0:05] 講者A：後講\n[0:09] 講者B：又是我", known)
    assert known == ["講者B", "講者A"]


def test_collect_speakers_appends_to_existing_without_duplicates():
    known = ["講者A"]
    collect_speakers("[0:00] 講者A：還是我\n[0:04] 講者C：新的人", known)
    assert known == ["講者A", "講者C"]


# ---- 跨段講者提示 ----

def test_speaker_hint_lists_known_speakers():
    hint = speaker_hint(["講者A", "講者B"])
    assert "講者A、講者B" in hint
    assert "沿用" in hint


def test_speaker_hint_none_when_no_speakers_yet():
    assert speaker_hint([]) is None


# ---- 時間戳平移 ----

def test_shift_timestamps_adds_offset():
    text = "[0:03] 講者A：開始\n[0:41] 講者B：補充"
    assert shift_timestamps(text, 45) == "[0:48] 講者A：開始\n[1:26] 講者B：補充"


def test_shift_timestamps_crosses_the_hour_mark():
    assert shift_timestamps("[0:10] 收尾", 3600).startswith("[1:00:10]")


def test_shift_timestamps_strips_when_offset_unknown():
    """不知道偏移量時，相對時間是錯的——寧可拿掉也不要誤導。"""
    assert shift_timestamps("[0:03] 講者A：哈囉", None) == "講者A：哈囉"


def test_shift_timestamps_marks_segment_start_when_model_gave_none():
    """模型沒標時間時，至少在段首補上本段開始時間，維持段落級時間軸。"""
    assert shift_timestamps("講者A：哈囉", 90) == "[1:30] 講者A：哈囉"


def test_shift_timestamps_leaves_unmarked_lines_alone():
    text = "[0:05] 講者A：有標時間\n這行沒有時間標記"
    assert shift_timestamps(text, 60) == "[1:05] 講者A：有標時間\n這行沒有時間標記"
