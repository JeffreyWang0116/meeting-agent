# 主動式會議 Agent

把混亂的會議討論（文字 / 音檔 / 影片 / 現場錄音）自動變成：**結構化任務清單、會議結論確認信草稿、行事曆事件**。

逢甲大學資訊工程學系專題 — 指導教授：薛念林教授

## 系統架構

```
① 文字貼上 ─────────────────────────────────┐
② 音檔/影片上傳                                │
   影片 → ffmpeg 抽音軌 →                      ├→ Parser Agent（正規化）
   faster-whisper 轉錄（GPU，進度即時回報）      │   → Decision Agent（Gemini → 結構化 JSON）
③ 即時聆聽                                     │   → Executor Agent（寫入任務庫）
   瀏覽器錄音，每 45 秒一段即時轉逐字稿 ─────────┘   → Notifier Agent（確認信草稿＋行事曆事件）
```

| 模組 | 位置 | 目前實作 | 之後升級 |
|------|------|----------|----------|
| 輸入與解析 | `app/agents/parser_agent.py`、`app/transcription/` | 本地 faster-whisper（GPU）或雲端 Gemini 轉錄，可切換 | — |
| 檢索與決策 | `app/agents/decision_agent.py` | Gemini 產出結構化 JSON | 10 月加 RAG（ChromaDB） |
| 資料庫與任務分發 | `app/agents/executor_agent.py`、`app/stores/` | 本地 JSON（`data/output/db.json`） | 8 月換 Firebase Firestore（實作 `TaskStore` 介面即可） |
| 時程同步與通知 | `app/agents/notifier_agent.py` | 產生信件草稿與事件 JSON 存本地 | 9 月串 Gmail / Google Calendar API |

### 轉錄後端可切換

`TRANSCRIBE_ENGINE` 決定轉錄怎麼做，兩者共用同一組介面（`transcribe / device / model_size`），靠 `create_app` 的依賴注入互換：

- `local`（預設）：本地 **faster-whisper**，需要 NVIDIA GPU，完全離線、不耗 API 額度——本機開發用這個
- `gemini`：把音訊丟給 **Gemini** 直接轉錄，不需要 GPU——雲端部署用這個（見下方「部署到雲端」）

## 快速開始

### 1. 前置需求（開發機已完成安裝）

- Python 3.13（Windows Store 版），虛擬環境在 `.venv/`
- ffmpeg（已用 `winget install Gyan.FFmpeg` 裝好；新開的終端機才抓得到 PATH）
- NVIDIA GPU 可加速轉錄；偵測不到 CUDA 會自動退回 CPU（功能不變，速度較慢）

### 2. 設定 Gemini API 金鑰

1. 到 <https://aistudio.google.com/apikey> 建立金鑰（免費）
2. 複製 `.env.example` 為 `.env`，填入 `GEMINI_API_KEY=你的金鑰`

### 3. 啟動

```powershell
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

打開 <http://localhost:8000> ，頁面上方會顯示環境狀態（金鑰、ffmpeg、Whisper 裝置）。

### 4. 測試

```powershell
.venv\Scripts\python -m pytest tests -q
```

所有測試都不需要網路、不需要 API 金鑰、不會載入 Whisper 模型（Gemini 與 Whisper 皆以注入的假物件測試）。

## 部署到雲端（給別人試用）

雲端主機沒有 GPU，所以部署版把轉錄從本地 Whisper 換成 Gemini（設定 `TRANSCRIBE_ENGINE=gemini`，`Dockerfile` 已預設）。文字貼上、檔案上傳、即時聆聽三種輸入都可用。

repo 已附 `Dockerfile`（含 ffmpeg）、`requirements-cloud.txt`（精簡依賴，不含 faster-whisper）與 `render.yaml` 藍圖。以 [Render](https://render.com) 免費方案為例：

1. 到 Render → **New → Blueprint**，連上這個 GitHub repo，它會自動讀 `render.yaml`
2. 部署過程會要你填 `GEMINI_API_KEY`（金鑰只存在 Render 後台，不進 repo）
3. 等 Docker build 完成，就會拿到一個公開網址（如 `https://meeting-agent.onrender.com`）

> 免費方案注意：閒置一段時間後容器會休眠，下次連線需等約 30 秒冷啟動；檔案系統是暫時性的（重啟後 `db.json` 會清空）。要永久保存任務資料，就是 8 月的 Firestore 那一步。

## 使用方式

- **文字貼上**：把會議紀錄 / 群組對話貼進文字框（`data/samples/` 有三份中英夾雜的模擬紀錄可以直接試）
- **檔案上傳**：支援 mp3 / wav / m4a / mp4 / mov / mkv 等；影片自動抽聲音軌，長檔會顯示轉錄進度與部分逐字稿
- **即時聆聽**：允許麥克風後開始，每 45 秒（可在 `.env` 調整）自動送出一段轉文字，逐字稿即時增長；按「結束會議」彙整全文分析
- **會議日期**欄位是相對日期（「下週五」）的換算基準，預設今天

分析結果會顯示：會議摘要、出席者、決議、代辦（負責人／期限／優先級／原文出處）、待確認事項（議而未決）、確認信草稿、行事曆事件。所有代辦同時寫入頁面底部的「資料庫」。

產出的檔案在 `data/output/`：
- `db.json` — 任務庫（會議 + 攤平的任務）
- `notifications/<meeting_id>/email_draft.txt` — 確認信草稿全文
- `notifications/<meeting_id>/calendar_events.json` — Google Calendar `events.insert` 可直接使用的事件格式

## 設計決策備忘

- **負責人不明的代辦**：`owner` 為 null 並自動列入待確認事項——不讓 LLM 硬猜，降低幻覺
- **每個代辦附 `source_quote`**（逐字稿原句），方便人工核對準確率
- **JSON 驗證失敗自動重試**：把 Pydantic 錯誤訊息回饋給 Gemini，最多 3 次
- **即時聆聽的分段策略**：每段用新的 MediaRecorder 錄（而非 `timeslice`），確保每段音訊都有完整檔頭、可獨立解碼
- **Whisper 首次執行會下載模型**（medium 約 1.5GB），之後走本地快取；轉錄完全離線、不耗 API 額度

## 專案結構

```
app/
├── main.py               # FastAPI 入口與所有 API 端點
├── config.py             # .env 設定
├── models.py             # MeetingAnalysis JSON schema（LLM 產出契約）
├── orchestrator.py       # Parser → Decision → Executor → Notifier
├── jobs.py               # 音檔/影片背景轉錄工作
├── agents/               # 四個核心 Agent
├── stores/               # TaskStore 介面 + 本地 JSON 實作
├── transcription/        # faster-whisper（本地）、gemini_transcriber（雲端）、ffmpeg、即時聆聽 session
└── static/index.html     # 前端（三分頁輸入 + 結果面板）
tests/                    # pytest 測試（84）
Dockerfile                # 雲端部署映像（Python + ffmpeg，轉錄用 Gemini）
render.yaml               # Render 一鍵部署藍圖
data/samples/             # 模擬會議紀錄（中英夾雜、含邊界案例）
data/output/              # 任務庫與通知產出
```
