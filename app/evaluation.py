"""任務抽取品質評估：預測 vs 人工標注的模糊比對與指標計算。

口試與報告的量化數據來源。任務層級用正規化後的字串相似度做貪婪配對
（同一件事的不同措辭也算對），欄位正確率（owner / due_date）只在
配對成功的任務上計算。本模組是純函式、不呼叫 LLM——真實模型輸出由
eval/run.py 產生後餵進來。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

_NOISE = re.compile(r"[\s,，。、；;：:！!？?〜~「」『』""''\"'()（）\[\]【】]+")


def _normalize(text) -> str:
    return _NOISE.sub("", str(text or "").lower())


def task_similarity(a, b) -> float:
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _field_equal(a, b) -> bool:
    return _normalize(a) == _normalize(b)  # 兩邊都空（null）也算一致


def match_todos(predicted: list[dict], gold: list[dict], threshold: float = 0.5) -> dict:
    """貪婪配對：相似度由高到低撮合，每筆預測/標注最多用一次。"""
    pairs = sorted(
        (
            (task_similarity(p.get("task"), g.get("task")), i, j)
            for i, p in enumerate(predicted)
            for j, g in enumerate(gold)
        ),
        reverse=True,
    )
    used_pred: set[int] = set()
    used_gold: set[int] = set()
    matches = []
    for sim, i, j in pairs:
        if sim < threshold:
            break
        if i in used_pred or j in used_gold:
            continue
        used_pred.add(i)
        used_gold.add(j)
        matches.append({"pred": i, "gold": j, "similarity": sim})

    return {
        "matches": matches,
        "tp": len(matches),
        "fp": len(predicted) - len(matches),
        "fn": len(gold) - len(matches),
        "owner_correct": sum(
            1
            for m in matches
            if _field_equal(predicted[m["pred"]].get("owner"), gold[m["gold"]].get("owner"))
        ),
        "due_correct": sum(
            1
            for m in matches
            if _field_equal(predicted[m["pred"]].get("due_date"), gold[m["gold"]].get("due_date"))
        ),
    }


def metrics(tp: int, fp: int, fn: int) -> dict:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def aggregate(results: list[dict]) -> dict:
    """跨題目 micro-average：先加總 tp/fp/fn 再算指標。"""
    tp = sum(r["tp"] for r in results)
    fp = sum(r["fp"] for r in results)
    fn = sum(r["fn"] for r in results)
    owner_correct = sum(r["owner_correct"] for r in results)
    due_correct = sum(r["due_correct"] for r in results)
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "owner_correct": owner_correct,
        "due_correct": due_correct,
        "owner_accuracy": owner_correct / tp if tp else 0.0,
        "due_accuracy": due_correct / tp if tp else 0.0,
        **metrics(tp, fp, fn),
    }
