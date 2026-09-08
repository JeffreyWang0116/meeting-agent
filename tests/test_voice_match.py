"""VoiceMatcher 測試：注入假的 generate，不呼叫真 API。

聲紋比對是「加分項」——會前錄的樣本對不上時，逐字稿維持講者代號仍然可用。
所以這裡的重點有兩個：對得上時要正確產生對應，以及**任何一種出錯都必須
安靜地回空 dict**，不能讓一場已經開完的會議分析失敗。
"""
import json
from pathlib import Path

from app.transcription.voice_match import VoiceMatcher

ENROLLMENTS = [
    {"name": "王小明", "path": Path("enroll_0.webm")},
    {"name": "李美華", "path": Path("enroll_1.webm")},
]
EVIDENCE = [
    {"path": Path("chunk_000.webm"), "transcript": "[0:01] 講者A：大家好\n[0:05] 講者B：你好"},
]


def _reply(mapping):
    return json.dumps(
        {"speakers": [
            {"label": k, "name": v, "evidence": "嗓音一致"} for k, v in mapping.items()
        ]},
        ensure_ascii=False,
    )


def _matcher(generate):
    return VoiceMatcher(api_key="k", generate=generate)


# ---- 正常路徑 ----

def test_matches_labels_to_enrolled_names():
    m = _matcher(lambda parts: _reply({"講者A": "王小明", "講者B": "李美華"}))
    assert m.match(ENROLLMENTS, EVIDENCE) == {"講者A": "王小明", "講者B": "李美華"}


def test_partial_match_is_kept():
    """只認得出一個人時，那一個仍然要生效，另一個維持代號。

    寧可補一半，也不要因為有人沒對到就整批放棄——沒對到的維持代號本來就可用。
    """
    m = _matcher(lambda parts: _reply({"講者A": "王小明"}))
    assert m.match(ENROLLMENTS, EVIDENCE) == {"講者A": "王小明"}


def test_audio_and_labels_are_interleaved_in_order():
    """送進模型的內容要「一段說明配一段音訊」，模型才知道哪段是誰。

    全部音訊擠在一起、說明放在最前面的話，模型無從得知第三個檔案是李美華的
    樣本還是會議片段。
    """
    captured = {}

    def capture(parts):
        captured["parts"] = parts
        return _reply({})

    _matcher(capture).match(ENROLLMENTS, EVIDENCE)
    parts = captured["parts"]
    audio_positions = [i for i, p in enumerate(parts) if isinstance(p, Path)]
    # 每個音訊前面都要緊接著一段介紹它的文字
    assert all(isinstance(parts[i - 1], str) for i in audio_positions)
    assert parts[audio_positions[0] - 1].find("王小明") >= 0
    assert parts[audio_positions[1] - 1].find("李美華") >= 0
    # 註冊樣本在前、會議片段在後，且逐字稿要跟著它那一段音訊
    assert "講者A" in parts[audio_positions[2] - 1]
    assert [p.name for p in parts if isinstance(p, Path)] == [
        "enroll_0.webm", "enroll_1.webm", "chunk_000.webm",
    ]


# ---- 只准對應到註冊過的人 ----

def test_names_outside_the_enrollment_list_are_dropped():
    """模型不可以自己發明第三個人。名冊之外的姓名一律丟掉。"""
    m = _matcher(lambda parts: _reply({"講者A": "王小明", "講者B": "路人甲"}))
    assert m.match(ENROLLMENTS, EVIDENCE) == {"講者A": "王小明"}


def test_unsafe_names_are_dropped():
    """含冒號、換行的姓名會在逐字稿裡造出假標籤，擋在這裡而不是等下游。"""
    m = _matcher(lambda parts: _reply({"講者A": "王小明：", "講者B": "李美華"}))
    assert m.match(ENROLLMENTS, EVIDENCE) == {"講者B": "李美華"}


def test_one_person_matched_to_two_labels_is_dropped():
    """同一個人不可能同時是講者A與講者B——分不出哪個對，兩個都不要。"""
    m = _matcher(lambda parts: _reply({"講者A": "王小明", "講者B": "王小明"}))
    assert m.match(ENROLLMENTS, EVIDENCE) == {}


def test_label_that_is_not_a_speaker_code_is_dropped():
    """label 必須是真的講者代號，不能是模型隨手寫的字串。"""
    m = _matcher(lambda parts: _reply({"主席": "王小明"}))
    assert m.match(ENROLLMENTS, EVIDENCE) == {}


# ---- 出錯一律回空 dict，不擋主流程 ----

def test_broken_json_returns_empty():
    assert _matcher(lambda parts: "這不是 JSON").match(ENROLLMENTS, EVIDENCE) == {}


def test_generate_failure_returns_empty():
    def boom(parts):
        raise RuntimeError("額度用完")

    assert _matcher(boom).match(ENROLLMENTS, EVIDENCE) == {}


def test_no_enrollments_skips_the_call_entirely():
    """沒錄樣本就完全不該打這個 API——這是選用功能，沒開就不能有成本。"""
    called = []
    m = _matcher(lambda parts: called.append(parts) or _reply({}))
    assert m.match([], EVIDENCE) == {}
    assert m.match(ENROLLMENTS, []) == {}
    assert called == []
