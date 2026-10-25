# 第一階段：穩定性與基礎

日期：2026-10-26　分支：`phase1-stability`

## 決策紀錄（使用者 2026-10-26 核准）
- 開匿名登入由使用者在 Firebase Console 操作；程式只負責「沒開時給看得懂的訊息、藏按鈕」
- 上傳轉錄的工作狀態落地，重啟後回報「中斷」並附已轉好的逐字稿，可直接拿去分析
- 問答索引只寫有變動的會議（原本每次存檔重寫全站所有記錄，吃 Firestore 寫入額度）
- 歷史會議列表前端先顯示 20 場＋「顯示更多」；不做伺服器端分頁（allMeetings 同時供標籤、問答範圍、任務分組使用）
- 隱私說明照實改寫；首次聆聽要確認與會者同意；可刪除帳號與全部資料（群組建立者須先處理群組）
- 逐字稿保留期限需要排程，移到第四階段與推播共用
- 網站原本是深色，補淺色模式：跟隨系統／淺色／深色
- 個人額度：`GET /api/quota` 回自己今天各項已用／上限

## 任務（每項先寫失敗測試）
1. **問答索引增量寫入**：`save_rag_records(dim, records, meeting_ids=None)`，帶 `meeting_ids` 時 Firestore 只查／刪／寫那幾場（`where meeting_id ==`），不整份掃描；`RAGIndex.sync／drop_meeting／reset(user)` 傳入變動的會議。驗證：假件計數寫入次數
2. **轉錄工作落地**：store 加 `save_job／get_job／delete_job／list_jobs(user)`；`MediaJobManager` 注入 store，狀態或逐字稿變動時寫入；記憶體查不到時讀 store，未結束者回 `status=error, interrupted=true` 並帶逐字稿；超過 1 天的紀錄讀到即刪，submit 時順手清自己的舊紀錄
3. **前端救援按鈕**：輪詢拿到 `interrupted` 且有逐字稿 → 「用已轉好的部分分析」走 `POST /api/meetings`
4. **health 回報進行中工作**：`active_work: {media_jobs, live_sessions}`；前端首次請求 3 秒未回顯示「伺服器喚醒中」
5. **歷史會議顯示更多**：一次 20 場
6. **隱私**：改寫頁尾；首次聆聽確認框（localStorage 每帳號記一次）；`DELETE /api/account` 刪會議／任務／詞彙／講者名冊／索引並退出群組，擁有群組時 409；設定加按鈕
7. **淺色模式**：補淺色 token、寫死色碼改變數；設定可選 系統／淺色／深色
8. **個人額度**：`RateLimiter.usage(user)`；`GET /api/quota`；設定顯示額度表、上傳鈕下顯示剩餘次數
9. **訪客登入沒開**：`auth/operation-not-allowed`／`auth/admin-restricted-operation` → 友善訊息、藏按鈕
10. README、完整測試、瀏覽器驗證
