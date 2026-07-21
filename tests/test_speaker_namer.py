"""SpeakerNamerAgent 測試：注入假的 generate，不呼叫真 API。

轉錄階段一律輸出「講者A/B/C」代號（模型沒有跨段記憶，允許它自由選用姓名
會導致同一個人在不同段落標成不同東西）。姓名改由這個 Agent 在最後一步，
拿整份逐字稿的上下文一次推斷出來並統一填回。
"""
import json

from app.agents.speaker_namer_agent import (
    SpeakerNamerAgent,
    apply_speaker_names,
)


def _reply(mapping):
    """把 {代號: 姓名} 包成模型會回傳的 JSON 形狀。"""
    return json.dumps(
        {"speakers": [
            {"label": k, "name": v, "evidence": "測試"} for k, v in mapping.items()
        ]},
        ensure_ascii=False,
    )


TRANSCRIPT = (
    "[0:05] 講者A：請問部長，中聯油脂案怎麼處理\n"
    "[0:31] 講者B：這件事衛福部已經在查了\n"
    "[1:02] 講者A：那時程呢"
)


# ---- 正常路徑 ----

def test_labels_are_replaced_with_inferred_names():
    agent = SpeakerNamerAgent(
        api_key="k",
        generate=lambda prompt: _reply({"講者A": "吳宗憲", "講者B": "石崇良"}),
    )
    text, applied = agent.name_speakers(TRANSCRIPT)
    assert "吳宗憲：請問部長" in text
    assert "石崇良：這件事" in text
    assert "講者A" not in text and "講者B" not in text
    assert {a["label"] for a in applied} == {"講者A", "講者B"}


def test_same_label_renamed_consistently_across_the_whole_transcript():
    """講者A 出現兩次，兩處都要換成同一個名字。"""
    agent = SpeakerNamerAgent(api_key="k", generate=lambda p: _reply({"講者A": "吳宗憲"}))
    text, _ = agent.name_speakers(TRANSCRIPT)
    assert text.count("吳宗憲：") == 2
    assert "講者B：" in text  # 沒被對應到的代號原樣保留


def test_unmapped_labels_are_left_as_codes():
    """推不出名字的講者維持代號，不該憑空編一個。"""
    agent = SpeakerNamerAgent(api_key="k", generate=lambda p: _reply({"講者A": "吳宗憲"}))
    text, applied = agent.name_speakers(TRANSCRIPT)
    assert "講者B：這件事" in text
    assert [a["label"] for a in applied] == ["講者A"]


# ---- 只動標籤，不動內容 ----

def test_content_mentions_of_the_code_are_not_rewritten():
    """有人在句子裡提到「講者A」時，那是說話內容，不是標籤。"""
    transcript = "[0:05] 講者A：剛剛講者A講的那件事我補充"
    agent = SpeakerNamerAgent(api_key="k", generate=lambda p: _reply({"講者A": "吳宗憲"}))
    text, _ = agent.name_speakers(transcript)
    assert text == "[0:05] 吳宗憲：剛剛講者A講的那件事我補充"


# ---- 防呆：擋掉會破壞逐字稿的對應 ----

def test_rejects_name_that_would_forge_a_new_label():
    """名字裡有冒號會在行首造出第二個假標籤。"""
    agent = SpeakerNamerAgent(api_key="k", generate=lambda p: _reply({"講者A": "王委員：他說"}))
    text, applied = agent.name_speakers(TRANSCRIPT)
    assert text == TRANSCRIPT and applied == []


def test_rejects_overlong_name():
    agent = SpeakerNamerAgent(api_key="k", generate=lambda p: _reply({"講者A": "王" * 40}))
    text, applied = agent.name_speakers(TRANSCRIPT)
    assert text == TRANSCRIPT and applied == []


def test_rejects_renaming_a_code_to_another_code():
    """講者A → 講者B 不是命名，是重新編號，只會製造混淆。"""
    agent = SpeakerNamerAgent(api_key="k", generate=lambda p: _reply({"講者A": "講者B"}))
    text, applied = agent.name_speakers(TRANSCRIPT)
    assert text == TRANSCRIPT and applied == []


def test_rejects_two_labels_mapping_to_the_same_person():
    """把兩個代號併成同一人是模型不該擅自做的判斷——寧可都留代號。"""
    agent = SpeakerNamerAgent(
        api_key="k", generate=lambda p: _reply({"講者A": "吳宗憲", "講者B": "吳宗憲"})
    )
    text, applied = agent.name_speakers(TRANSCRIPT)
    assert text == TRANSCRIPT and applied == []


def test_ignores_labels_not_present_in_the_transcript():
    agent = SpeakerNamerAgent(api_key="k", generate=lambda p: _reply({"講者Z": "路人"}))
    text, applied = agent.name_speakers(TRANSCRIPT)
    assert text == TRANSCRIPT and applied == []


# ---- 失敗時不擋住流程（與 CorrectorAgent 同一套原則）----

def test_model_error_returns_transcript_unchanged():
    def boom(prompt):
        raise RuntimeError("API 掛了")

    text, applied = SpeakerNamerAgent(api_key="k", generate=boom).name_speakers(TRANSCRIPT)
    assert text == TRANSCRIPT and applied == []


def test_garbage_response_returns_transcript_unchanged():
    agent = SpeakerNamerAgent(api_key="k", generate=lambda p: "這不是 JSON")
    text, applied = agent.name_speakers(TRANSCRIPT)
    assert text == TRANSCRIPT and applied == []


def test_empty_transcript_is_not_sent_to_the_model():
    calls = []
    agent = SpeakerNamerAgent(api_key="k", generate=lambda p: calls.append(p) or _reply({}))
    assert agent.name_speakers("") == ("", [])
    assert calls == []


# ---- 結構保證 ----

def test_line_count_and_time_markers_are_preserved():
    agent = SpeakerNamerAgent(
        api_key="k", generate=lambda p: _reply({"講者A": "吳宗憲", "講者B": "石崇良"})
    )
    text, _ = agent.name_speakers(TRANSCRIPT)
    assert text.count("\n") == TRANSCRIPT.count("\n")
    for marker in ("[0:05]", "[0:31]", "[1:02]"):
        assert marker in text


def test_apply_speaker_names_is_usable_without_the_model():
    """本地套用邏輯獨立可測，不需要 LLM。"""
    text, applied = apply_speaker_names(TRANSCRIPT, {"講者A": "吳宗憲"})
    assert text.count("吳宗憲：") == 2
    assert applied[0]["count"] == 2


# ---- prompt ----

def test_prompt_demands_evidence_and_forbids_guessing():
    """沒有依據就不要對應——編一個名字比留著代號更糟。"""
    prompt = SpeakerNamerAgent(api_key="k").build_prompt(TRANSCRIPT)
    assert "依據" in prompt
    assert "講者A" in prompt          # 逐字稿本身要進 prompt
    assert "中聯油脂案" in prompt
