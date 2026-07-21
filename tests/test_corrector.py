"""Corrector Agent 測試：以注入的假 generate 函式取代真實 Gemini 呼叫。

重點不在「模型有沒有找對錯字」（那是模型能力），而在「模型亂回傳時，
逐字稿會不會被弄壞」——時間標記與行結構是會議重點跳轉的依據，不能被動到。
"""
import json

from app.agents.corrector_agent import CorrectorAgent, apply_corrections

TRANSCRIPT = "[0:05] 講者A：這個涵式要重寫。\n[1:02] 王林祥：我下週處理。"


def fake(corrections) -> str:
    return json.dumps({"corrections": corrections}, ensure_ascii=False)


def agent(corrections, **kw) -> CorrectorAgent:
    return CorrectorAgent(generate=lambda prompt: fake(corrections), **kw)


def test_applies_correction_and_reports_it():
    text, applied = agent([{"wrong": "涵式", "right": "函式", "reason": "同音字"}]).correct(
        TRANSCRIPT
    )
    assert "函式" in text and "涵式" not in text
    assert applied[0]["wrong"] == "涵式"
    assert applied[0]["right"] == "函式"
    assert applied[0]["count"] == 1


def test_replaces_every_occurrence_and_counts_them():
    src = "涵式一\n涵式二"
    text, applied = agent([{"wrong": "涵式", "right": "函式"}]).correct(src)
    assert text == "函式一\n函式二"
    assert applied[0]["count"] == 2


def test_timestamps_and_line_structure_preserved():
    text, _ = agent([{"wrong": "王林祥", "right": "王霖翔"}]).correct(TRANSCRIPT)
    assert text.startswith("[0:05] ")
    assert "[1:02] 王霖翔：" in text
    assert text.count("\n") == TRANSCRIPT.count("\n")


def test_empty_corrections_returns_original():
    text, applied = agent([]).correct(TRANSCRIPT)
    assert text == TRANSCRIPT
    assert applied == []


def test_empty_transcript_short_circuits():
    """空逐字稿不該浪費一次 API 請求。"""
    calls = []

    def spy(prompt):
        calls.append(prompt)
        return fake([])

    text, applied = CorrectorAgent(generate=spy).correct("   ")
    assert text == "   " and applied == [] and calls == []


def test_llm_failure_falls_back_to_original():
    """校正是加分項：模型爆炸不該擋住整場分析。"""

    def boom(prompt):
        raise RuntimeError("Gemini 掛了")

    text, applied = CorrectorAgent(generate=boom).correct(TRANSCRIPT)
    assert text == TRANSCRIPT and applied == []


def test_invalid_json_falls_back_to_original():
    text, applied = CorrectorAgent(generate=lambda p: "我覺得沒有錯字").correct(TRANSCRIPT)
    assert text == TRANSCRIPT and applied == []


def test_code_fence_stripped():
    raw = "```json\n" + fake([{"wrong": "涵式", "right": "函式"}]) + "\n```"
    text, applied = CorrectorAgent(generate=lambda p: raw).correct(TRANSCRIPT)
    assert "函式" in text and len(applied) == 1


def test_glossary_terms_enter_prompt():
    a = CorrectorAgent(generate=lambda p: fake([]), glossary=lambda: [
        {"term": "王霖翔", "note": "人名"}
    ])
    prompt = a.build_prompt(TRANSCRIPT)
    assert "王霖翔（人名）" in prompt
    assert "詞彙表" not in CorrectorAgent(generate=lambda p: fake([])).build_prompt("x")


# ---- 防呆：模型亂回傳時不能破壞逐字稿 ----

def test_correction_touching_timestamp_rejected():
    """模型想改時間標記一律拒絕：會議重點的跳轉靠它定位。"""
    text, applied = agent([{"wrong": "[0:05]", "right": "[0:07]"}]).correct(TRANSCRIPT)
    assert text == TRANSCRIPT and applied == []


def test_multiline_correction_rejected():
    """跨行取代會把兩行併成一行，破壞一句一行的時間軸結構。"""
    text, applied = agent(
        [{"wrong": "重寫。\n[1:02] 王林祥：", "right": "重寫。王霖翔："}]
    ).correct(TRANSCRIPT)
    assert text == TRANSCRIPT and applied == []


def test_correction_not_present_in_transcript_skipped():
    text, applied = agent([{"wrong": "不存在的字", "right": "別的字"}]).correct(TRANSCRIPT)
    assert text == TRANSCRIPT and applied == []


def test_overlong_correction_rejected():
    """長字串代表模型想改寫句子，不是修錯字。"""
    long_wrong = "這個涵式要重寫。" * 10
    text, applied = agent([{"wrong": long_wrong, "right": "短"}]).correct(
        long_wrong + "\n第二行"
    )
    assert applied == []


def test_noop_and_empty_corrections_skipped():
    text, applied = agent(
        [{"wrong": "涵式", "right": "涵式"}, {"wrong": "", "right": "x"}]
    ).correct(TRANSCRIPT)
    assert text == TRANSCRIPT and applied == []


def test_line_count_change_aborts_whole_batch():
    """萬一有漏網之魚改變了行數，整批放棄——寧可不校正也不交出壞掉的逐字稿。"""
    text, applied = apply_corrections("一行\n兩行", [{"wrong": "一行", "right": "一\n行"}])
    assert text == "一行\n兩行" and applied == []


def test_corrections_capped():
    many = [{"wrong": f"字{i}", "right": f"詞{i}"} for i in range(150)]
    src = "".join(f"字{i}" for i in range(150))
    _, applied = agent(many).correct(src)
    assert len(applied) <= 100
