# 主動式會議 Agent

把混亂的會議討論（文字 / 音檔 / 影片 / 現場錄音）自動變成：**結構化任務清單、會議結論確認信草稿、行事曆事件**，並支援跨會議語意問答。

## 功能總覽

- **三種輸入**：貼文字、上傳音檔/影片、瀏覽器即時錄音；可選**錄音種類**（會議/通話/訪談/語音備忘錄/講座/其它），AI 依種類調整分析重點
- **Messenger 式逐字稿**：一句一個對話泡泡、講者自動分色；即時聆聽、檔案上傳、歷史查閱三處一致；一鍵複製全文
- **即時翻譯（中↔英）**：錄音時逐段翻譯、泡泡內雙語對照；摘要也可一鍵翻譯
- **自訂詞彙表**（設定 ⚙ 內管理）：人名、產品名等專有名詞注入轉錄與分析，不再聽錯寫錯
- **任務庫**：狀態切換、列內編輯（名稱/負責人/期限）、**手動新增任務**、刪除、CSV 匯出
- **歷史會議**：查閱（AI 摘要＋決議＋泡泡逐字稿）、編輯、**重新分析**、講者一鍵改名（**同步更新出席者與任務負責人**）、分享到其他 App（Web Share）、下載 Markdown、**.ics 加入行事曆**、刪除
- **分類標籤**：AI 自動建議、可自訂，歷史列表按標籤/種類篩選
- **詢問會議**：語意問答（RAG）可**複選會議範圍**；同一個輸入框打字即時做**關鍵字搜尋**，點擊直接開啟該場會議
- **主動提醒**：逾期/即將到期/未指派任務與未決事項自動擬催辦草稿，可選開啟**每日瀏覽器通知**
- **資料備份**（設定 ⚙ 內）：一鍵下載整份資料 JSON、可再匯入還原（雲端暫時性磁碟的保險）
- **PWA**：手機可「加入主畫面」，以近原生方式使用

## 系統架構

```
① 文字貼上 ─────────────────────────────────┐
② 音檔/影片上傳                                │
   影片 → ffmpeg 抽音軌 →                      ├→ Parser Agent（正規化）
   faster-whisper 轉錄（GPU，進度即時回報）      │   → Corrector Agent（校正錯字，選用）
③ 即時聆聽                                     │   → Decision Agent（Gemini → 結構化 JSON）
   瀏覽器錄音，每 45 秒一段即時轉逐字稿 ─────────┘   → Executor Agent（寫入任務庫）
                                                   → Notifier Agent（確認信草稿＋行事曆事件）
```

| 模組 | 位置 | 目前實作 | 之後升級 |
|------|------|----------|----------|
| 輸入與解析 | `app/agents/parser_agent.py`、`app/transcription/` | 本地 faster-whisper（GPU）或雲端 Gemini 轉錄，可切換 | — |
| 錯字校正（選用） | `app/agents/corrector_agent.py` | Gemini 找出同音錯字，回傳修正清單在本地套用 | — |
| 檢索與決策 | `app/agents/decision_agent.py` | Gemini 產出結構化 JSON | — |
| 跨會議問答（RAG） | `app/rag.py` | Gemini 向量嵌入 + 語意檢索，跨所有會議回答提問 | — |
| 資料庫與任務分發 | `app/agents/executor_agent.py`、`app/stores/` | 本地 JSON（`data/output/db.json`）；填 Firebase 金鑰即自動改用 Firestore 雲端持久化 | ✅ Firestore 已接（`FirestoreStore` 實作同一 `TaskStore` 介面） |
| 時程同步與通知 | `app/agents/notifier_agent.py` | 產生信件草稿與事件 JSON 存本地 | 9 月串 Gmail / Google Calendar API |

### 轉錄後端可切換

`TRANSCRIBE_ENGINE` 決定轉錄怎麼做，兩者共用同一組介面（`transcribe / device / model_size`），靠 `create_app` 的依賴注入互換：

- `local`（預設）：本地 **faster-whisper**，需要 NVIDIA GPU，完全離線、不耗 API 額度——本機開發用這個
- `gemini`：把音訊丟給 **Gemini** 直接轉錄，不需要 GPU——雲端部署用這個（見下方「部署到雲端」）

### 用到的模型與免費額度

轉錄與分析是**兩個獨立的 Gemini 模型設定**，可各自用環境變數覆蓋：

| 用途 | 環境變數 | 預設模型 | 免費額度（每專案每日）|
|------|----------|----------|----------------------|
| 音訊轉錄 | `TRANSCRIBE_MODEL` | `gemini-flash-lite-latest` | 高（Flash Lite 約 500 次/日、15 次/分）|
| 長音檔分段秒數 | `TRANSCRIBE_CHUNK_SECONDS` | `240`（0＝不分段） | 每段各算一次轉錄請求 |
| 會議分析、跨會議問答 | `GEMINI_MODEL` | `gemini-flash-lite-latest` | 同上 |
| 錯字校正（選用） | `CORRECT_MODEL` | `gemini-flash-lite-latest` | 同上 |

> **為什麼分兩個設定**：即時聆聽每 45 秒轉錄一次，吃掉絕大多數請求；分析每場會議只呼叫 1~3 次。兩者拆開，就能各自挑模型、額度互不排擠。
>
> **關於 429（配額爆掉）**：免費層每日額度是**「每專案每模型」共用**——同一個 Google 專案下的多把 key 共用同一份額度，加 key 只增加每分鐘吞吐、不增加每日總量。要更高每日量：改用**不同專案**的 key，或升級付費。實際額度看 [AI Studio](https://aistudio.google.com/rate-limit)。
>
> **品質 vs 額度**：想要更好的分析品質，可把 `GEMINI_MODEL` 設為 `gemini-3.5-flash`（推理較強，但免費層每日僅 20 次，適合少量分析）。
>
> **多人會議的講者分辨**：轉錄的 prompt 已強力要求標註講者，但實測 `gemini-flash-lite` 對「誰在講話」的辨識仍不穩定，3 人以上時常被併成一兩位。需要準確標出多位講者時，把 `TRANSCRIBE_MODEL` 設為 `gemini-flash-latest`（可正確分出多位講者，代價是免費每日額度較低）。
>
> **長音檔會自動分段轉錄**：實測把整份 17 分鐘的質詢錄音丟給 `gemini-flash-lite`，講者標註會**整份消失**、時間戳還會漂到比實際長度多 3 分鐘；同一支影片只取前 3 分鐘卻能正確分出講者A/B/C。模型在長音訊上顯然會放棄逐句標註，所以超過 6 分鐘的音檔會先用 ffmpeg 切成每段 `TRANSCRIBE_CHUNK_SECONDS`（預設 240 秒）再逐段轉錄，最後把時間戳平移回整場時間、講者標籤跨段沿用同一組。代價是每段各算一次 API 請求（17 分鐘的檔約 5 次）。設成 `0` 可關閉分段、回到整份送出的舊行為。

### 可靠性

- **多把金鑰輪替**：`GEMINI_API_KEYS`（逗號分隔）round-robin，每次呼叫換下一把；撞 429 自動跳下一把
- **503 過載自動退避重試**：Google 端暫時過載時指數退避（1s→2s→4s）重試最多 3 次
- **JSON 驗證失敗自動重試**：把 Pydantic 錯誤訊息回饋給模型，最多 3 次

## 快速開始

### 1. 前置需求（開發機已完成安裝）

- Python 3.13（Windows Store 版），虛擬環境在 `.venv/`
- ffmpeg（已用 `winget install Gyan.FFmpeg` 裝好；新開的終端機才抓得到 PATH）
- NVIDIA GPU 可加速轉錄；偵測不到 CUDA 會自動退回 CPU（功能不變，速度較慢）

### 2. 設定 Gemini API 金鑰

1. 到 <https://aistudio.google.com/apikey> 建立金鑰（免費）
2. 複製 `.env.example` 為 `.env`，填入 `GEMINI_API_KEY=你的金鑰`
   （有多把想輪替，改填 `GEMINI_API_KEYS=key1,key2,...`，逗號分隔）

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
2. 部署過程會要你填 `GEMINI_API_KEY`（金鑰只存在 Render 後台，不進 repo）；多把 key 就改設 `GEMINI_API_KEYS`
3. 等 Docker build 完成，就會拿到一個公開網址（如 `https://meeting-agent.onrender.com`）

`render.yaml` 已預設好雲端需要的環境變數（`TRANSCRIBE_ENGINE=gemini`、轉錄與分析模型皆為 `gemini-flash-lite-latest`）；金鑰類（`GEMINI_API_KEY`、`FIREBASE_CREDENTIALS_JSON`、`API_TOKEN`）標記 `sync: false`，不進 repo、由你在 Render 後台填。

> 免費方案注意：閒置一段時間後容器會休眠，下次連線需等約 30 秒冷啟動；檔案系統是暫時性的（重啟後 `db.json` 會清空）。要**永久保存任務資料**，加設 `FIREBASE_CREDENTIALS_JSON` 環境變數（見下方）即可切成 Firestore。

#### 加上 API 認證（強烈建議，部署後網址是公開的）

不設 `API_TOKEN` 的話，任何人只要知道你的 Render 網址，就能直接呼叫 `/api/backup` 下載全部會議紀錄、`/api/restore` 覆蓋資料庫，或刪除任意會議/任務——沒有密碼也沒有登入頁。

1. 在 Render 環境變數新增 `API_TOKEN`，填一串隨機字串（例如用 `openssl rand -hex 32` 產生）
2. 重新部署後，所有 `/api/*` 端點（除了 `/api/health`）都會要求帶 `Authorization: Bearer <token>`
3. 開啟網頁時，前端第一次打 API 會收到 401，跳出輸入框貼上這串 token 即可（存在瀏覽器 `localStorage`，之後不用再輸入）

本機開發預設不設 `API_TOKEN`，不會要求登入。

#### （選填）用 Firestore 永久保存資料

不設定就是本地 JSON，Render 重啟會清空；設定後所有會議與代辦改存 Google Firestore，重新部署也不會遺失。

1. 到 [Firebase Console](https://console.firebase.google.com) 建專案 → **Firestore Database** 按 **建立資料庫**（正式或測試模式皆可，本服務用 Admin SDK 直連不受安全規則影響）
2. **專案設定 → 服務帳戶 → 產生新的私密金鑰**，下載一份 service account JSON
3. 在 Render 環境變數新增 `FIREBASE_CREDENTIALS_JSON`，把整份 JSON 內容貼進去（單行、含大括號即可）
4. 重新部署。啟動後 `GET /api/health` 的 `store_backend` 會顯示 `firestore` 代表已生效

> 本機開發若要連 Firestore，改設 `FIREBASE_CREDENTIALS_FILE=/path/to/service-account.json`（指向檔案路徑），並先 `pip install firebase-admin`。

## 使用方式

- **文字貼上**：把會議紀錄 / 群組對話貼進文字框（`data/samples/` 有三份中英夾雜的模擬紀錄可以直接試）
- **檔案上傳**：支援 mp3 / wav / m4a / mp4 / mov / mkv 等；影片自動抽聲音軌，長檔會顯示轉錄進度與部分逐字稿
- **即時聆聽**：允許麥克風後開始，每 45 秒（可在 `.env` 調整）自動送出一段轉文字，逐字稿即時增長；按「結束會議」彙整全文分析
- **會議日期**欄位是相對日期（「下週五」）的換算基準，預設今天
- **AI 校正錯字**（預設關閉）：勾選後在分析前多跑一次 AI，用上下文修掉語音辨識的同音錯字（「涵式」→「函式」）。改了哪些字會列在結果最下方，隨時可核對

分析結果會顯示：會議摘要、會議重點（帶時間節點，點擊可跳到逐字稿出處）、出席者、決議、代辦（負責人／期限／優先級／原文出處）、待確認事項（議而未決）、確認信草稿、行事曆事件。所有代辦同時寫入頁面底部的「資料庫」。

產出的檔案在 `data/output/`：
- `db.json` — 任務庫（會議 + 攤平的任務）
- `notifications/<meeting_id>/email_draft.txt` — 確認信草稿全文
- `notifications/<meeting_id>/calendar_events.json` — Google Calendar `events.insert` 可直接使用的事件格式

## 設計決策備忘

- **負責人不明的代辦**：`owner` 為 null 並自動列入待確認事項——不讓 LLM 硬猜，降低幻覺
- **每個代辦附 `source_quote`**（逐字稿原句），方便人工核對準確率
- **錯字校正回傳「修正清單」而非整份逐字稿**：讓模型重寫全文，它會順手刪贅字、拿掉時間標記與講者標籤，時間標記一掉會議重點的跳轉就壞了。改成只回 `{wrong, right}` 清單、在本地字串取代，並驗證行數與時間標記數量不變（不符就整批放棄）——輸出 token 也少很多，且使用者看得到改了什麼
- **JSON 驗證失敗自動重試**：把 Pydantic 錯誤訊息回饋給 Gemini，最多 3 次
- **即時聆聽的分段策略**：每段用新的 MediaRecorder 錄（而非 `timeslice`），確保每段音訊都有完整檔頭、可獨立解碼
- **Whisper 首次執行會下載模型**（medium 約 1.5GB），之後走本地快取；轉錄完全離線、不耗 API 額度

## 程式碼地圖（哪個功能在哪裡）

### 後端（Python / FastAPI）

```
app/
├── main.py               # FastAPI 入口：所有 API 端點、features 解析、API 認證
├── config.py             # .env 設定（金鑰、轉錄引擎、儲存後端…）
├── models.py             # MeetingAnalysis JSON schema（LLM 產出契約，含 Highlight）
├── orchestrator.py       # Parser → Decision → Executor → Notifier 串接
├── agents/
│   ├── parser_agent.py       # 輸入清洗（零寬字元、空行）
│   ├── corrector_agent.py    # 轉錄後錯字校正（選用）：回傳修正清單，本地套用
│   ├── decision_agent.py     # LLM 分析：摘要/會議重點/決議/代辦 prompt 與重試
│   ├── executor_agent.py     # 分析結果寫入任務庫
│   ├── notifier_agent.py     # 確認信草稿 + 行事曆事件
│   └── reminder_agent.py     # 主動提醒（逾期/將到期/未指派掃描）
├── transcription/
│   ├── transcriber.py        # 本地 faster-whisper 轉錄
│   ├── gemini_transcriber.py # 雲端 Gemini 轉錄（時間戳 prompt、長音檔自動分段）
│   ├── live_session.py       # 即時聆聽 session：並發配位、逐段累積
│   ├── segments.py           # 分段結果縫合：時間戳平移、跨段講者一致性
│   └── media.py              # ffmpeg 抽音軌、取長度、切段
├── stores/               # TaskStore 介面 + 本地 JSON / Firestore 兩種實作
├── jobs.py               # 音檔/影片背景轉錄工作佇列
├── rag.py                # 跨會議問答（向量嵌入 + 語意檢索 + AskAgent）
├── translate.py          # 即時翻譯（逐段/摘要）
├── glossary.py           # 自訂詞彙表（轉錄與分析 prompt 共用）
├── export.py             # 匯出：任務 CSV、行事曆 .ics、Markdown 會議報告
├── usage.py              # 今日 API 用量統計
├── gemini_keys.py        # 多金鑰輪替 + 429/503 重試
├── timeutil.py           # 本地時區（UTC+8）工具
├── atomicio.py           # 原子寫檔（斷電不壞資料）
└── evaluation.py         # 任務抽取 precision/recall（供 eval/run.py）
eval/                     # 量化評估：標注資料集 + 評估腳本
tests/                    # pytest 測試（288，全部離線、不需金鑰）
Dockerfile                # 雲端部署映像（Python + ffmpeg，轉錄用 Gemini）
render.yaml               # Render 一鍵部署藍圖
data/samples/             # 模擬會議紀錄（中英夾雜、含邊界案例）
data/output/              # 任務庫與通知產出
```

### 前端（app/static/，三個檔案功能分區順序一致）

```
app/static/
├── index.html            # 畫面結構（每個 panel 有註解標示）
├── style.css             # 樣式（檔頭有分區目錄）
├── app.js                # 行為（檔頭有編號目錄，1~10 大區）
├── icon.svg / manifest.webmanifest / sw.js   # PWA
```

app.js 的十大分區（style.css 依畫面順序對應）：

| # | 分區 | 內容 |
| --- | --- | --- |
| 1 | 共用基礎 | API 認證 fetch、SVG 圖示、esc/錯誤橫幅 |
| 2 | 全域初始化 | 會議日期、錄音種類、功能勾選、分頁切換 |
| 3 | 逐字稿 | 連續文件式渲染（時間欄＋講者＋內文）、時間/引用句跳轉 |
| 4 | 分析結果 | 摘要、會議重點（點擊跳轉）、決議、代辦、行事曆、確認信 |
| 5 | 任務庫 | 清單、搜尋篩選、列內編輯、手動新增 |
| 6 | 歷史會議 | 查閱、編輯、重新分析、分享、講者改名、刪除 |
| 7 | 主動提醒 | 到期掃描與每日通知 |
| 8 | 跨會議問答 | RAG 問答＋關鍵字即時搜尋 |
| 9 | 輸入路徑 | 純文字貼上、檔案上傳（含拖曳）、即時聆聽 |
| 10 | 介面與資料工具 | 面板收縮、主題、設定選單、自訂詞彙、備份還原、PWA |
