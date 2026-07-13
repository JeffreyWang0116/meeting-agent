"""量化評估：用真實 Gemini 對標注測試集抽取任務，計算 precision/recall/F1。

用法（會消耗 Gemini 配額，每題 1 次請求；免費層每天每把 key 20 次）：
    .venv\\Scripts\\python -m eval.run            # 跑全部 10 題
    .venv\\Scripts\\python -m eval.run --limit 3  # 只跑前 3 題（省配額）

結果印在終端機並寫入 eval/report.md，作為專題報告/口試的量化數據。
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

from app.agents.decision_agent import DecisionAgent
from app.config import get_settings
from app.evaluation import aggregate, match_todos, metrics

DATASET = Path(__file__).parent / "dataset.jsonl"
REPORT = Path(__file__).parent / "report.md"


def load_dataset() -> list[dict]:
    lines = DATASET.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines]


def main() -> None:
    parser = argparse.ArgumentParser(description="任務抽取量化評估")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 題（省配額）")
    parser.add_argument("--model", default=None, help="覆蓋 .env 的 GEMINI_MODEL")
    parser.add_argument("--threshold", type=float, default=0.5, help="任務比對相似度門檻")
    args = parser.parse_args()

    settings = get_settings()
    model = args.model or settings.gemini_model
    agent = DecisionAgent(
        api_key=settings.gemini_api_key,
        api_keys=settings.gemini_api_keys,
        model=model,
    )

    items = load_dataset()[: args.limit]
    print(f"模型：{model}｜題數：{len(items)}（消耗 {len(items)} 次 Gemini 請求）\n")

    rows, results = [], []
    for item in items:
        analysis = agent.analyze(
            item["transcript"], meeting_date=date.fromisoformat(item["meeting_date"])
        )
        predicted = [t.model_dump(mode="json") for t in analysis.todos]
        result = match_todos(predicted, item["gold_todos"], threshold=args.threshold)
        results.append(result)
        m = metrics(result["tp"], result["fp"], result["fn"])
        rows.append(
            f"| {item['id']} | {len(item['gold_todos'])} | {len(predicted)} "
            f"| {result['tp']} | {m['precision']:.2f} | {m['recall']:.2f} "
            f"| {result['owner_correct']}/{result['tp']} | {result['due_correct']}/{result['tp']} |"
        )
        print(f"[{item['id']}] 標注 {len(item['gold_todos'])}、預測 {len(predicted)}、"
              f"配對 {result['tp']}（P {m['precision']:.2f} / R {m['recall']:.2f}）")

    agg = aggregate(results)
    summary = (
        f"\n== 總體（micro-average, {len(items)} 題）==\n"
        f"任務抽取  precision {agg['precision']:.3f}｜recall {agg['recall']:.3f}｜F1 {agg['f1']:.3f}\n"
        f"欄位正確率（配對成功者）  負責人 {agg['owner_accuracy']:.3f}｜期限 {agg['due_accuracy']:.3f}"
    )
    print(summary)

    REPORT.write_text(
        "# 任務抽取量化評估報告\n\n"
        f"- 執行時間：{datetime.now():%Y-%m-%d %H:%M}\n"
        f"- 模型：{model}\n"
        f"- 題數：{len(items)}｜比對門檻：{args.threshold}\n\n"
        "| 題目 | 標注數 | 預測數 | 配對 | P | R | 負責人 | 期限 |\n"
        "|---|---|---|---|---|---|---|---|\n"
        + "\n".join(rows)
        + "\n\n## 總體（micro-average）\n\n"
        f"| precision | recall | F1 | 負責人正確率 | 期限正確率 |\n"
        f"|---|---|---|---|---|\n"
        f"| {agg['precision']:.3f} | {agg['recall']:.3f} | {agg['f1']:.3f} "
        f"| {agg['owner_accuracy']:.3f} | {agg['due_accuracy']:.3f} |\n",
        encoding="utf-8",
    )
    print(f"\n報告已寫入 {REPORT}")


if __name__ == "__main__":
    main()
