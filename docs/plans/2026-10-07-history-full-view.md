# 歷史會議用分析結果視窗完整檢視

日期：2026-10-07

## 決策紀錄（使用者 2026-10-07 核准）
- 共用「分析結果」視窗顯示歷史會議，左上角「← 返回歷史會議」
- 保留列表原地展開（編輯、重新分析、分享、詞彙替換等歷史專屬功能留在那裡），另加「完整檢視」按鈕

## 任務
1. 後端 `GET /api/meetings/{id}/notifications`：用會議紀錄＋任務庫現況產確認信主旨／草稿、行事曆事件
   （`notifier_agent.analysis_from_record`，`email_draft_from_record` 改用它）。事件 id 與分析當下相同，
   「先前已加過」的判斷才不會失效。測試：tests/test_api.py
2. 前端 `renderResult(result, transcript, { kind, fromHistory })`：歷史模式不跑詞彙升級、不刷新清單、
   種類用會議自己的；顯示返回鈕，新分析時隱藏
3. 列表「完整檢視」按鈕：取會議＋任務＋通知 → 組 result → renderResult；返回鍵回歷史會議列表原頁
4. README、全套測試、瀏覽器實測
