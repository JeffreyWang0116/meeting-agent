"""講者代號換姓名：只套用使用者預錄聲音時填的姓名，不從對話內容推斷。

系統曾經有「辨識名稱」選項，讓模型讀整份逐字稿猜誰是誰。實測（立法院質詢，
翁曉玲詢問王榮璋）模型把講者自己說的「主席好」「謝謝王委員的提問」當成他本人，
被批評的陳菊也被安成講者。整條已移除：講者一律維持代號，由使用者事後改名。
"""
from app.orchestrator import Orchestrator
from app.transcription.speaker_names import apply_speaker_names, is_safe_name

TRANSCRIPT = (
    "[0:05] 講者A：請問部長，中聯油脂案怎麼處理\n"
    "[0:31] 講者B：這件事衛福部已經在查了\n"
    "[1:02] 講者A：那時程呢"
)


# ---- 本地套用 ----

def test_label_is_renamed_consistently_and_other_codes_stay():
    text, applied = apply_speaker_names(TRANSCRIPT, {"講者A": "吳宗憲"})
    assert text.count("吳宗憲：") == 2
    assert "講者B：這件事" in text
    assert applied == [{"label": "講者A", "name": "吳宗憲", "count": 2}]


def test_content_mentions_of_the_code_are_not_rewritten():
    """有人在句子裡提到「講者A」時，那是說話內容，不是標籤。"""
    text, _ = apply_speaker_names("[0:05] 講者A：剛剛講者A講的那件事我補充", {"講者A": "吳宗憲"})
    assert text == "[0:05] 吳宗憲：剛剛講者A講的那件事我補充"


def test_line_count_and_time_markers_are_preserved():
    text, _ = apply_speaker_names(TRANSCRIPT, {"講者A": "吳宗憲", "講者B": "石崇良"})
    assert text.count("\n") == TRANSCRIPT.count("\n")
    for marker in ("[0:05]", "[0:31]", "[1:02]"):
        assert marker in text


def test_unsafe_names_are_rejected():
    assert not is_safe_name("王委員：他說")  # 冒號會在行首造出第二個假標籤
    assert not is_safe_name("王" * 40)
    assert not is_safe_name("講者B")  # 對應成另一個代號是重新編號
    assert not is_safe_name("[0:01]")
    assert is_safe_name("吳宗憲")


def test_any_bad_mapping_abandons_the_whole_batch():
    """一半代號一半姓名的逐字稿比全部維持代號更難讀。"""
    for mapping in (
        {"講者A": "吳宗憲", "講者B": "王委員：他說"},
        {"講者A": "吳宗憲", "講者B": "吳宗憲"},
        {"講者A": "吳宗憲", "講者Z": "路人"},
    ):
        assert apply_speaker_names(TRANSCRIPT, mapping) == (TRANSCRIPT, [])


# ---- pipeline：只有預錄姓名會自動套用 ----

class _Parser:
    def parse(self, t):
        return t


class _Analysis:
    def model_dump(self, mode=None):
        return {}


class _Decision:
    def __init__(self):
        self.text = None

    def analyze(self, text, **kw):
        self.text = text
        return _Analysis()


class _Executor:
    def execute(self, *a, **kw):
        return "m1"


class _Notifier:
    def notify(self, *a, **kw):
        return {}


def _pipeline():
    decision = _Decision()
    return Orchestrator(
        parser=_Parser(), decision=decision, executor=_Executor(), notifier=_Notifier()
    ), decision


def test_speakers_stay_as_codes_without_a_prior():
    pipeline, decision = _pipeline()
    result = pipeline.process_transcript(TRANSCRIPT)
    assert result["transcript"] == TRANSCRIPT
    assert decision.text == TRANSCRIPT
    assert result["speaker_names"] == []


def test_prior_names_are_applied_before_analysis():
    """預錄比對出的姓名是使用者自己填的，分析與存檔都吃換好的版本。"""
    pipeline, decision = _pipeline()
    result = pipeline.process_transcript(TRANSCRIPT, speaker_prior={"講者A": "吳宗憲"})
    assert "吳宗憲：請問部長" in result["transcript"]
    assert "講者B：這件事" in result["transcript"]  # 沒比對到的維持代號，留給使用者改名
    assert decision.text == result["transcript"]
    assert result["speaker_names"] == [{"label": "講者A", "name": "吳宗憲", "count": 2}]


def test_unsafe_prior_names_are_ignored():
    """prior 的姓名一樣要過安全檢查，不能因為來源不同就跳過。"""
    pipeline, _ = _pipeline()
    result = pipeline.process_transcript(
        TRANSCRIPT, speaker_prior={"講者A": "吳宗憲：主席", "講者B": "石崇良"}
    )
    assert "講者A：請問部長" in result["transcript"]
    assert "石崇良：這件事" in result["transcript"]


def test_orchestrator_has_no_name_guessing_option():
    """釘住「不要再長回來」：pipeline 不接受任何從對話內容推斷姓名的開關。"""
    import inspect

    params = inspect.signature(Orchestrator.process_transcript).parameters
    assert "name_speakers" not in params
    assert "namer" not in inspect.signature(Orchestrator.__init__).parameters
