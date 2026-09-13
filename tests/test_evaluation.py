"""任務抽取評估邏輯測試：預測與人工標注的模糊比對、P/R/F1 計算。

評估本身不呼叫 LLM——eval/run.py 拿真實 Gemini 輸出餵進這裡的純函式。
"""
import pytest

from app.evaluation import aggregate, match_todos, metrics, task_similarity


# ---- 相似度 ----

def test_identical_tasks_full_similarity():
    assert task_similarity("完成 Prompt 初版", "完成 Prompt 初版") == 1.0


def test_similarity_ignores_spacing_case_punctuation():
    assert task_similarity("完成 PROMPT 初版。", "完成prompt初版") == 1.0


def test_unrelated_tasks_low_similarity():
    assert task_similarity("完成 Prompt 初版", "訂會議室") < 0.3


# ---- 比對 ----

def gold(task, owner=None, due=None):
    return {"task": task, "owner": owner, "due_date": due}


def test_exact_match_counts_tp_and_fields():
    pred = [gold("完成 Prompt 初版", "陳志明", "2026-07-20")]
    g = [gold("完成 Prompt 初版", "陳志明", "2026-07-20")]
    r = match_todos(pred, g)
    assert (r["tp"], r["fp"], r["fn"]) == (1, 0, 0)
    assert r["owner_correct"] == 1
    assert r["due_correct"] == 1


def test_fuzzy_match_above_threshold():
    pred = [gold("寫好 Prompt 的第一版")]
    g = [gold("完成 Prompt 初版")]
    assert match_todos(pred, g, threshold=0.4)["tp"] == 1


def test_no_match_below_threshold():
    r = match_todos([gold("訂會議室")], [gold("完成 Prompt 初版")])
    assert (r["tp"], r["fp"], r["fn"]) == (0, 1, 1)


def test_wrong_owner_still_matches_task_but_not_field():
    r = match_todos([gold("完成 Prompt 初版", "Kevin")], [gold("完成 Prompt 初版", "陳志明")])
    assert r["tp"] == 1
    assert r["owner_correct"] == 0


def test_both_owner_none_counts_correct():
    r = match_todos([gold("完成 Prompt 初版")], [gold("完成 Prompt 初版")])
    assert r["owner_correct"] == 1


def test_greedy_matching_no_double_use():
    """兩個預測搶同一個標注時，只有最像的配對成功，另一個算 FP。"""
    pred = [gold("完成 Prompt 初版"), gold("完成 Prompt")]
    g = [gold("完成 Prompt 初版")]
    r = match_todos(pred, g)
    assert (r["tp"], r["fp"], r["fn"]) == (1, 1, 0)
    assert r["matches"][0]["similarity"] == 1.0


# ---- 指標 ----

def test_metrics_basic():
    m = metrics(tp=8, fp=2, fn=2)
    assert m["precision"] == pytest.approx(0.8)
    assert m["recall"] == pytest.approx(0.8)
    assert m["f1"] == pytest.approx(0.8)


def test_metrics_zero_division_safe():
    m = metrics(tp=0, fp=0, fn=0)
    assert m == {"precision": 0.0, "recall": 0.0, "f1": 0.0}


def test_aggregate_micro_averages_across_items():
    items = [
        match_todos([gold("完成 Prompt 初版")], [gold("完成 Prompt 初版")]),  # tp=1
        match_todos([gold("訂便當")], [gold("部署 Render 環境")]),            # fp=1 fn=1
    ]
    agg = aggregate(items)
    assert (agg["tp"], agg["fp"], agg["fn"]) == (1, 1, 1)
    assert agg["precision"] == pytest.approx(0.5)
    assert agg["recall"] == pytest.approx(0.5)


def test_curly_quotes_are_treated_as_punctuation():
    """模型輸出常用中文彎引號；原本字元集合裡的 “” ‘’ 被寫成 ASCII 引號，
    raw 字串還因此斷成兩段拼接，彎引號完全沒被濾掉，同一件事被當成不同任務。"""
    from app.evaluation import _field_equal

    assert _field_equal("“完成”報告", "完成報告")
    assert _field_equal("‘志明’", "志明")
