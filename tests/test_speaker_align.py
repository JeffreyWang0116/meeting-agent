"""講者對齊測試：把 pyannote 的「誰在何時講話」依時間戳填回 Gemini 逐字稿。

Gemini 分段轉錄沒有跨段記憶，講者代號跨段會亂跳；pyannote 對整份音檔一次
分群，代號全場一致。這組純函式負責把兩者接起來，是整個混合式設計的核心。
"""
from app.transcription.speaker_align import (
    code_map,
    relabel,
    speaker_prior_from_identify,
    to_session_time,
)


def seg(speaker, start, end):
    return {"speaker": speaker, "start": start, "end": end}


# ---- 代號映射 ----

def test_code_map_orders_by_first_appearance_not_by_pyannote_id():
    segments = [seg("SPEAKER_03", 10, 12), seg("SPEAKER_00", 20, 25), seg("SPEAKER_03", 30, 31)]
    assert code_map(segments) == {"SPEAKER_03": "講者A", "SPEAKER_00": "講者B"}


def test_code_map_ignores_input_order():
    segments = [seg("SPEAKER_01", 50, 60), seg("SPEAKER_02", 5, 8)]
    assert code_map(segments) == {"SPEAKER_02": "講者A", "SPEAKER_01": "講者B"}


def test_code_map_beyond_26_speakers_uses_numbers_the_parser_accepts():
    segments = [seg(f"SPEAKER_{i:02d}", i, i + 0.5) for i in range(28)]
    codes = code_map(segments)
    assert codes["SPEAKER_25"] == "講者Z"
    assert codes["SPEAKER_26"] == "講者27"
    assert codes["SPEAKER_27"] == "講者28"


# ---- 最大重疊歸屬 ----

def test_relabel_replaces_gemini_label_with_diarized_speaker():
    transcript = "[0:00] 講者A：大家好\n[0:05] 講者A：我是另一個人"
    segments = [seg("SPEAKER_00", 0, 4.8), seg("SPEAKER_01", 5.1, 9)]
    text, stats = relabel(transcript, segments, duration=10)
    assert text == "[0:00] 講者A：大家好\n[0:05] 講者B：我是另一個人"
    assert stats["matched"] == 2


def test_relabel_adds_label_to_line_gemini_left_unlabelled():
    transcript = "[0:00] 講者A：主席請\n[0:04] 謝謝主席"
    segments = [seg("SPEAKER_00", 0, 3.5), seg("SPEAKER_01", 4, 8)]
    text, _ = relabel(transcript, segments, duration=10)
    assert text.split("\n")[1] == "[0:04] 講者B：謝謝主席"


def test_relabel_picks_speaker_with_most_overlap_not_the_one_at_line_start():
    # 時間戳只到秒：[0:10] 開頭 0.5 秒還是前一位的尾音，這行實際是 SPEAKER_01 講的
    transcript = "[0:10] 講者A：這一行\n[0:20] 講者A：下一行"
    segments = [seg("SPEAKER_00", 0, 10.5), seg("SPEAKER_01", 10.5, 19.5), seg("SPEAKER_00", 20, 25)]
    text, _ = relabel(transcript, segments, duration=30)
    assert text.split("\n")[0] == "[0:10] 講者B：這一行"


def test_relabel_sums_overlap_across_several_turns_of_same_speaker():
    transcript = "[0:00] 講者A：交錯發言\n[0:10] 講者A：下一行"
    segments = [
        seg("SPEAKER_00", 0, 2), seg("SPEAKER_01", 2, 5),
        seg("SPEAKER_00", 5, 7), seg("SPEAKER_00", 7.5, 9.5),
        seg("SPEAKER_01", 10, 15),
    ]
    text, _ = relabel(transcript, segments, duration=20)
    # SPEAKER_00 共 6 秒 > SPEAKER_01 的 3 秒
    assert text.split("\n")[0] == "[0:00] 講者A：交錯發言"


def test_relabel_gives_same_second_lines_at_least_one_second():
    transcript = "[0:03] 講者A：好\n[0:03] 講者B：請\n[0:09] 講者A：謝謝"
    segments = [seg("SPEAKER_00", 3, 3.6), seg("SPEAKER_01", 3.6, 8)]
    text, stats = relabel(transcript, segments, duration=10)
    assert stats["unmatched"] == 0
    assert text.split("\n")[1].startswith("[0:03] 講者")


# ---- 最後一行、超出音檔 ----

def test_relabel_last_line_uses_capped_window():
    transcript = "[0:00] 講者A：開場\n[1:00] 講者A：結尾"
    # 結尾那行後面 15 秒內是 SPEAKER_01；更遠處 SPEAKER_00 講很久也不該算進來
    segments = [seg("SPEAKER_00", 0, 50), seg("SPEAKER_01", 60, 70), seg("SPEAKER_00", 80, 200)]
    text, _ = relabel(transcript, segments, duration=300)
    assert text.split("\n")[1] == "[1:00] 講者B：結尾"


def test_relabel_drops_lines_stamped_beyond_audio_duration():
    # PoC 實測：休會靜音段 lite 模型重複吐同幾句，時間戳漂到 [5:24:40]，遠超過音檔長度
    transcript = "[0:00] 講者A：休息十分鐘\n[5:24:40] 講者A：休息十分鐘\n[0:30] 講者B：繼續"
    segments = [seg("SPEAKER_00", 0, 5), seg("SPEAKER_01", 30, 35)]
    text, stats = relabel(transcript, segments, duration=60)
    assert text == "[0:00] 講者A：休息十分鐘\n[0:30] 講者B：繼續"
    assert stats["dropped"] == 1


def test_relabel_without_duration_never_drops():
    transcript = "[0:00] 講者A：一\n[9:00:00] 講者A：二"
    segments = [seg("SPEAKER_00", 0, 5)]
    text, stats = relabel(transcript, segments, duration=None)
    assert len(text.split("\n")) == 2
    assert stats["dropped"] == 0


def test_dropped_line_takes_its_continuation_lines_with_it():
    transcript = "[0:00] 講者A：一\n[9:00:00] 講者A：幻覺\n幻覺續行\n[0:20] 講者A：三"
    segments = [seg("SPEAKER_00", 0, 5), seg("SPEAKER_00", 20, 25)]
    text, _ = relabel(transcript, segments, duration=60)
    assert "幻覺" not in text


# ---- 靜音、容差 ----

def test_relabel_uses_nearby_turn_within_tolerance():
    transcript = "[0:10] 講者A：好\n[0:11] 講者A：下一句"
    # [10,11) 完全沒有語音，但 2 秒後就有 SPEAKER_01 開口
    segments = [seg("SPEAKER_00", 0, 5), seg("SPEAKER_01", 12.9, 20)]
    text, stats = relabel(transcript, segments, duration=30, tolerance=3)
    assert text.split("\n")[0] == "[0:10] 講者B：好"
    assert stats["tolerance"] == 1


def test_relabel_strips_label_when_no_speech_nearby():
    transcript = "[0:30] 講者A：沒人講話的地方\n[1:00] 講者A：有人"
    segments = [seg("SPEAKER_00", 0, 5), seg("SPEAKER_00", 60, 65)]
    text, stats = relabel(transcript, segments, duration=100, tolerance=3)
    assert text.split("\n")[0] == "[0:30] 沒人講話的地方"
    assert stats["unmatched"] == 1


# ---- 不動的東西 ----

def test_relabel_keeps_continuation_lines_and_original_time_prefix():
    transcript = "[1:02] 講者C：第一句\n沒有時間戳的續行\n[1:10]講者C：第二句"
    segments = [seg("SPEAKER_07", 62, 69), seg("SPEAKER_02", 70, 80)]
    text, _ = relabel(transcript, segments, duration=100)
    assert text == "[1:02] 講者A：第一句\n沒有時間戳的續行\n[1:10]講者B：第二句"


def test_relabel_leaves_mid_sentence_colon_alone():
    transcript = "[0:00] 重點：這不是講者標籤"
    segments = [seg("SPEAKER_00", 0, 5)]
    text, _ = relabel(transcript, segments, duration=10)
    assert text == "[0:00] 講者A：重點：這不是講者標籤"


def test_relabel_without_timestamps_returns_transcript_unchanged():
    # Whisper 本地引擎的輸出沒有時間戳，無從對齊
    transcript = "大家好\n今天開會"
    text, stats = relabel(transcript, [seg("SPEAKER_00", 0, 5)], duration=10)
    assert text == transcript
    assert stats == {"matched": 0, "tolerance": 0, "unmatched": 0, "dropped": 0}


def test_relabel_without_segments_returns_transcript_unchanged():
    # 整份沒偵測到語音時，寧可保留 Gemini 的標註，也不要把標籤全部拔光
    transcript = "[0:00] 講者A：有字"
    text, _ = relabel(transcript, [], duration=10)
    assert text == transcript


# ---- identify → speaker_prior ----

def test_speaker_prior_maps_matches_through_code_map():
    codes = {"SPEAKER_01": "講者A", "SPEAKER_00": "講者B"}
    voiceprints = [
        {"speaker": "SPEAKER_00", "match": "王小明", "confidence": {"王小明": 88}},
        {"speaker": "SPEAKER_01", "match": "李大華", "confidence": {"李大華": 91}},
    ]
    assert speaker_prior_from_identify(voiceprints, codes) == {"講者B": "王小明", "講者A": "李大華"}


def test_speaker_prior_skips_unmatched_and_unknown_speakers():
    codes = {"SPEAKER_00": "講者A"}
    voiceprints = [
        {"speaker": "SPEAKER_00", "match": None, "confidence": {}},
        {"speaker": "SPEAKER_09", "match": "幽靈", "confidence": {"幽靈": 99}},
    ]
    assert speaker_prior_from_identify(voiceprints, codes) == {}


# ---- 即時聆聽：串接檔時間 → 整場時間 ----

def test_to_session_time_shifts_each_segment_by_its_chunk_offset():
    # 兩段各 45 秒；第二段前端回報從整場 50 秒開始（中間斷了 5 秒）
    placements = [(0.0, 45.0, 0.0), (45.0, 90.0, 50.0)]
    segments = [seg("SPEAKER_00", 10, 20), seg("SPEAKER_01", 50, 60)]
    assert to_session_time(segments, placements) == [
        seg("SPEAKER_00", 10, 20), seg("SPEAKER_01", 55, 65),
    ]


def test_to_session_time_splits_segment_crossing_chunk_boundary():
    placements = [(0.0, 45.0, 0.0), (45.0, 90.0, 50.0)]
    segments = [seg("SPEAKER_00", 40, 50)]
    assert to_session_time(segments, placements) == [
        seg("SPEAKER_00", 40, 45), seg("SPEAKER_00", 50, 55),
    ]
