"""分段逐字稿縫合工具測試。

即時聆聽與長音檔上傳都靠這組函式把「各自獨立轉錄的段落」接回一份
連續的逐字稿——時間戳要平移回整場時間，講者標籤要跨段沿用同一組。
"""
from app.transcription.segments import (
    chunk_hint,
    collect_speakers,
    drop_lines_before,
    format_time,
    drop_empty_lines,
    normalize_timestamps,
    parse_time_label,
    replace_speaker,
    shift_timestamps,
    speaker_label_ratio,
    speaker_of,
    transcript_tail,
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
    assert speaker_of("[1:02:03] 講者B: hello") == "講者B"


def test_speaker_of_without_time_prefix():
    assert speaker_of("講者B：換我說") == "講者B"


def test_speaker_of_returns_none_when_unlabelled():
    assert speaker_of("[0:05] 這句沒有講者標籤") is None
    assert speaker_of("") is None


# ---- 只認代號、不認名字 ----
# 轉錄階段一律用「講者A/B/C」代號，真實姓名交由事後對應處理。理由是模型沒有
# 跨段記憶：同一個人在聽得到名字的段落標「王委員」、聽不到的段落標「講者A」，
# 逐字稿就會出現同一人兩種標籤。把名字排除在標籤之外，來源就只剩一種。

def test_speaker_of_accepts_canonical_labels_only():
    assert speaker_of("講者A：內容") == "講者A"
    assert speaker_of("說話者B：內容") == "說話者B"
    assert speaker_of("Speaker C: content") == "SpeakerC"
    assert speaker_of("講者1：內容") == "講者1"


def test_speaker_of_normalises_spacing_and_case():
    """「講者 a」與「講者A」是同一個人，不能在跨段清單裡佔兩格。"""
    assert speaker_of("講者 a：內容") == "講者A"
    assert speaker_of("講者A：內容") == "講者A"


def test_personal_name_is_not_a_speaker_label():
    """名字不算標籤——它是事後對應的產物，不是轉錄階段的輸出。"""
    assert speaker_of("[0:05] Kevin: hello") is None
    assert speaker_of("[0:31] 王委員：請問部長") is None


# ---- 句中冒號不得被誤判成講者（回歸測試）----
# 舊的樣式只要開頭 12 字內有冒號就當作講者，中文逐字稿裡到處都是這種句子。
# 後果是連鎖的：標註率被灌水到 0.83 而高於重試門檻，模型明明整段沒標講者，
# 系統卻判定「標得很好」不再重試；同時這些句子碎片被存進跨段講者清單，
# 下一段的提示就變成「先前已出現的講者：我想請問部長、重點」而越滾越髒。

def test_sentence_with_internal_colon_is_not_a_speaker():
    assert speaker_of("[0:12] 我想請問部長：這個預算是怎麼編的") is None
    assert speaker_of("[0:20] 好，那我這樣講：第一點是這樣") is None
    assert speaker_of("[0:45] 重點：三個月內完成") is None


def test_label_ratio_is_not_inflated_by_sentence_colons():
    text = (
        "[0:12] 我想請問部長：這個預算是怎麼編的\n"
        "[0:20] 好，那我這樣講：第一點是這樣\n"
        "[0:45] 重點：三個月內完成\n"
        "[1:20] 講者A：我同意"
    )
    # 修正前是 1.0（四行全被當成有講者）→ 高於門檻，重試永遠不會觸發
    assert speaker_label_ratio(text) == 0.25


def test_collect_speakers_ignores_sentence_fragments():
    known = []
    collect_speakers("[0:12] 我想請問部長：這個預算\n[0:20] 講者A：我回答", known)
    assert known == ["講者A"]


# ---- 把代號換成真實姓名 ----
# 轉錄階段一律輸出代號，姓名在最後一步統一填回。改寫必須只動行首標籤：
# 內文裡提到的「講者A」（例如有人說「剛剛講者A講的」）不能被一起換掉。

def test_replace_speaker_keeps_time_prefix():
    assert replace_speaker("[0:05] 講者A：你好", "王委員") == "[0:05] 王委員：你好"


def test_replace_speaker_without_time_prefix():
    assert replace_speaker("講者B：換我說", "卓榮泰") == "卓榮泰：換我說"


def test_replace_speaker_leaves_unlabelled_lines_alone():
    assert replace_speaker("[0:05] 這行沒有講者標籤", "王委員") == "[0:05] 這行沒有講者標籤"


def test_replace_speaker_only_touches_the_label_not_the_content():
    line = "[0:05] 講者A：剛剛講者A說的那件事"
    assert replace_speaker(line, "王委員") == "[0:05] 王委員：剛剛講者A說的那件事"


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


# ---- 畸形時間戳容錯 ----
# 實測 gemini-flash-lite 開場第一行常吐出「[00]」而不是「[0:00]」。
# 不認得的話會被當成「沒有時間標記」，分段平移時又補一個上去 → 兩個時間戳疊在一起。

def test_bare_number_timestamp_is_recognised():
    assert speaker_of("[00] 講者A：開場") == "講者A"
    assert parse_time_label("00") == 0
    assert parse_time_label("15") == 15


def test_bare_number_timestamp_shifts_without_duplicating():
    assert shift_timestamps("[00] 開頭那一大段", 240) == "[4:00] 開頭那一大段"
    # 回歸測試：修正前會變成 "[4:00] [00] 開頭那一大段"
    assert "[00]" not in shift_timestamps("[00] 開頭那一大段", 240)


def test_normalize_rewrites_malformed_marker_without_changing_time():
    """整份轉錄不會經過平移，也要把 [00] 正規化成前端認得的格式。"""
    assert normalize_timestamps("[00] 第一句") == "[0:00] 第一句"
    assert normalize_timestamps("[75] 第一句") == "[1:15] 第一句"


def test_normalize_leaves_well_formed_and_unmarked_lines_alone():
    assert normalize_timestamps("[1:02] 好的") == "[1:02] 好的"
    assert normalize_timestamps("沒有時間標記") == "沒有時間標記"


def test_normalize_never_prepends_a_marker():
    """normalize 只改寫既有標記，不像 shift 會在段首補標記。"""
    assert normalize_timestamps("第一行\n第二行") == "第一行\n第二行"


# ---- 分段提示 ----

def test_chunk_hint_always_demands_speaker_labels():
    """主 prompt 允許「整段同一人可不標註」，分段模式要關掉這個例外。"""
    hint = chunk_hint([])
    assert "只有一位講者" in hint and "不可省略" in hint


def test_chunk_hint_appends_known_speakers_when_available():
    hint = chunk_hint(["講者A", "講者B"])
    assert "不可省略" in hint          # 仍然要求標註
    assert "講者A、講者B" in hint      # 且沿用既有標籤


# ---- 單位數時間戳（模型實際會吐 [0:1]）----
# 認不得的話會連鎖出三個問題：時間解析不到、平移時疊上第二個時間戳、
# 講者擷取被時間戳的冒號截斷成「[0」而污染跨段講者清單。

def test_single_digit_second_is_recognised():
    assert normalize_timestamps("[0:1] 第一句") == "[0:01] 第一句"
    assert normalize_timestamps("[14:4] 尾聲") == "[14:04] 尾聲"


def test_single_digit_second_does_not_break_speaker_extraction():
    assert speaker_of("[0:1] 講者A：內容") == "講者A"
    assert speaker_of("[14:4] 講者B：內容") == "講者B"


def test_single_digit_second_shifts_without_duplicating():
    assert shift_timestamps("[0:1] 開場", 240) == "[4:01] 開場"
    assert "[0:1]" not in shift_timestamps("[0:1] 開場", 240)


def test_bracket_residue_never_becomes_a_speaker_name():
    """萬一又冒出沒被認出的時間戳寫法，也不能把「[0」當成講者存進清單。"""
    assert speaker_of("[0:1:2:3] 講者A：內容") is None
    known = []
    collect_speakers("[9:9:9:9] 講者A：內容", known)
    assert known == []


# ---- 講者標註率（重試判斷依據）----

def test_label_ratio_counts_labelled_lines():
    text = "[0:00] 講者A：一\n[0:05] 沒有標籤\n[0:10] 講者B：三\n[0:15] 也沒有"
    assert speaker_label_ratio(text) == 0.5


def test_label_ratio_all_or_nothing():
    assert speaker_label_ratio("[0:00] 講者A：一\n[0:05] 講者A：二") == 1.0
    assert speaker_label_ratio("[0:00] 沒標\n[0:05] 也沒標") == 0.0


def test_label_ratio_ignores_blank_lines():
    assert speaker_label_ratio("[0:00] 講者A：一\n\n\n[0:05] 講者B：二") == 1.0


def test_label_ratio_of_empty_text_is_not_a_failure():
    """空白段（靜音）不該被當成標註失敗而觸發重試。"""
    assert speaker_label_ratio("") == 1.0
    assert speaker_label_ratio("   ") == 1.0


# ---- 有標籤卻沒內容的行不算標好（回歸測試）----
# 實測台語質詢：某個 chunk 模型整段放棄轉錄，只吐出時間戳與空的「講者A：」，
# 例如「[10:09] 講者A：」後面什麼都沒有。舊的 ratio 只看行首有沒有標籤，
# 把這種空行算成「標好了」→ 標註率虛高、重試永遠不觸發，畫面被灌一堆空標籤。

def test_label_ratio_ignores_labelled_but_empty_lines():
    assert speaker_label_ratio("[0:00] 講者A：\n[0:05] 講者A：") == 0.0
    assert speaker_label_ratio("[0:00] 講者A：有內容\n[0:05] 講者A：") == 0.5


def test_label_ratio_ignores_punctuation_only_lines():
    """只剩「。」或「.」的行是模型放棄轉錄的殘骸，不算標好。"""
    assert speaker_label_ratio("[0:00] 講者A：真的有講\n[0:05] 講者A：。") == 0.5
    assert speaker_label_ratio("[0:00] 講者A：。\n[0:05] 講者B：.") == 0.0


def test_label_ratio_still_counts_labelled_lines_with_content():
    assert speaker_label_ratio("[0:00] 講者A：一\n[0:05] 講者B：二") == 1.0


# ---- 移除沒有實際內容的行 ----

def test_drop_empty_lines_removes_labelled_empties():
    text = "[0:00] 講者A：開場\n[0:05] 講者A：\n[0:09] 講者A：接著說"
    assert drop_empty_lines(text) == "[0:00] 講者A：開場\n[0:09] 講者A：接著說"


def test_drop_empty_lines_removes_timestamp_only_and_blank_lines():
    assert drop_empty_lines("[0:00] 講者A：有話\n[0:05] \n\n[0:10] ") == "[0:00] 講者A：有話"


def test_drop_empty_lines_keeps_plain_content_without_markers():
    """純貼上、沒有時間戳也沒有標籤但有文字的行要保留。"""
    assert drop_empty_lines("鈺翔：我下週交\n\nKevin：好") == "鈺翔：我下週交\nKevin：好"


def test_drop_empty_lines_keeps_unlabelled_content():
    assert drop_empty_lines("[0:04] 沒有講者但有內容") == "[0:04] 沒有講者但有內容"


def test_drop_empty_lines_removes_punctuation_only_lines():
    text = "[0:00] 講者A：真的有講\n[0:05] 講者A：。\n[0:09] 講者B：.\n[0:12] 講者A：又講"
    assert drop_empty_lines(text) == "[0:00] 講者A：真的有講\n[0:12] 講者A：又講"


def test_drop_empty_lines_keeps_short_interjection_with_a_word():
    """「啊。」有實際字（啊），不是純標點，要保留。"""
    assert drop_empty_lines("[0:00] 講者A：啊。") == "[0:00] 講者A：啊。"


# ---- 重疊區去重與跨段對照樣本 ----
# 模型沒聽過前一段，光給講者名單無從對應嗓音，同一個人跨段就會換標籤。
# 解法是每段往前多抓一小段音訊，並把前一段結尾的逐字稿當對照樣本給它。

def test_drop_lines_before_removes_overlap_region():
    """重疊那段前一輪已經轉過了，依絕對時間濾掉避免內容重複。"""
    text = "[3:45] 講者C：重疊區的話\n[3:58] 講者C：還是重疊區\n[4:02] 講者A：這才是本段的內容"
    assert drop_lines_before(text, 240) == "[4:02] 講者A：這才是本段的內容"


def test_drop_lines_before_keeps_boundary_line():
    assert drop_lines_before("[4:00] 剛好在界線上", 240) == "[4:00] 剛好在界線上"


def test_drop_lines_before_keeps_untimed_lines():
    """沒有時間戳就無從判斷，寧可留著也不要誤刪內容。"""
    text = "沒有時間戳\n[3:50] 重疊區\n[4:10] 本段"
    assert drop_lines_before(text, 240) == "沒有時間戳\n[4:10] 本段"


def test_transcript_tail_returns_last_lines():
    text = "\n".join(f"[0:{i:02d}] 講者A：第{i}句" for i in range(10))
    tail = transcript_tail(text, max_lines=3)
    assert tail.count("\n") == 2
    assert "第9句" in tail and "第7句" in tail and "第6句" not in tail


def test_transcript_tail_ignores_blank_lines():
    assert transcript_tail("[0:00] 一\n\n\n[0:05] 二", max_lines=2) == "[0:00] 一\n[0:05] 二"


def test_chunk_hint_includes_previous_tail_for_voice_matching():
    """提示要附上重疊處的對照樣本，模型才能把聲音對回既有標籤。"""
    hint = chunk_hint(["講者A", "講者C"], previous_tail="[3:55] 講者C：前一段的結尾")
    assert "[3:55] 講者C：前一段的結尾" in hint
    assert "重疊" in hint and "不要重新編號" in hint


def test_chunk_hint_without_tail_is_unchanged():
    """第一段沒有前文可對照，不該憑空提到重疊。"""
    hint = chunk_hint([])
    assert "重疊" not in hint
    assert "不可省略" in hint  # 但仍要求標註講者
