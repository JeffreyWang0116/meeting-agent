# 講者改名擴及所有 AI 文字 實作計畫

日期：2026-09-30

## 決策紀錄（使用者 2026-09-30 核准）
- 改名範圍新增：會議重點（text）、決議（description、context）、待確認（topic、reason）、
  種類專屬欄位（sections[].items）、任務名稱（task）與優先原因（priority_reason）
- 不動：`source_quote`（逐字稿內文不改名，引句一改就跳不回出處）、種類欄位標題、會議標題、
  確認信草稿與行事曆事件（使用者未選）
- 實作：新增後端端點 `POST /api/meetings/{id}/rename-speaker {old, new}`，一次改完會議與任務；
  改名邏輯移植成 Python（`app/speaker_rename.py`），刪掉前端 speakers.js 的三支改名函式，
  只保留 `speakerNameProblem` 做即時提示

## 任務
1. `app/speaker_rename.py` 純函式＋`tests/test_speaker_rename.py`（移植 JS 的 node 測試案例）
   - `rename_speaker_in_transcript`、`rename_in_list`、`rename_in_text`、`name_problem`
   - `rename_meeting_fields(record, old, new) -> dict`、`rename_task_fields(task, old, new) -> dict`
2. 端點＋API 測試（tests/test_api.py）：各欄位都換、source_quote 不動、別場會議的任務不動、
   新名不合格 400、找不到 404、RAG 索引作廢；回 `{"meeting": 更新後紀錄, "tasks": 該場任務}`
3. 前端：`api.renameSpeaker`；`renameSpeaker` 改打新端點；結果頁把重點／決議／待確認／種類欄位
   拆成可單獨重畫的函式，改名後重畫；刪 speakers.js 改名函式與對應 node 測試
4. README（功能總覽、設計決策、程式碼地圖）；全套 pytest＋ruff＋瀏覽器實測
