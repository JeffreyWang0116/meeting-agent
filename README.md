# 會議助手

把混亂的會議討論（文字 / 音檔 / 影片 / 現場錄音）自動變成：**結構化任務清單、會議結論確認信草稿、行事曆事件**，並支援跨會議語意問答。

## 功能總覽

- **三種輸入**：貼文字、上傳音檔/影片、瀏覽器即時錄音；可選**會議種類**（一般會議/一對一/語音備忘錄/專案會議/需求訪談/銷售拜訪/面試/其它）
- **種類專屬產出**：AI 依種類調整分析重點與預設勾選的區塊，並額外產出該種類專用的重點欄位——銷售拜訪出 BANT、面試按評估面向並附對話佐證、一對一看目標與回饋、需求訪談抓痛點與限制、專案會議追里程碑與風險。一般會議與「其它」刻意維持通用格式
- **Messenger 式逐字稿**：一句一個對話泡泡、講者自動分色；即時聆聽、檔案上傳、歷史查閱三處一致；一鍵複製全文
- **即時翻譯（中↔英）**：錄音時逐段翻譯、泡泡內雙語對照；摘要也可一鍵翻譯
- **自訂詞彙表**（設定 ⚙ 內管理）：人名、產品名等專有名詞注入轉錄與分析，不再聽錯寫錯。**依帳號各自獨立，而且只收你自己輸入的詞**——沒設定就是空的，講者一律維持「講者A/B/C」代號，不會冒出任何人名
- **講者一律標代號、結束後自己改名**：系統不從對話內容猜姓名（講者口中的「主席」「王委員」指的是對話另一方，AI 會標錯）。分析一結束，結果頁就能點講者改名（歷史會議也可以），逐字稿講者欄、出席者、摘要、負責人剛好是該代號的任務一起更新；重點、決議等其他 AI 文字不動。唯一會自動填姓名的是下面的預錄聲音辨識人——名字是你自己登記的
- **預錄聲音辨識人**（即時聆聽，選用）：開始聆聽前請每位與會者各錄約 10 秒並標上姓名（人不在現場就改上傳一段他的語音訊息當樣本），結束分析時用聲紋比對把逐字稿的「講者A/B/C」換成真實姓名。錄之前可先測麥克風音量，錄完會驗樣本品質（太短、幾乎沒聲音、說話時間太少、破音都會當場擋下）；聽到第二段就會先比對一次並回報「已認出誰」，結束時再回報「預錄 N 位、認出哪幾位、沒認出哪幾位」。**沒勾選就完全不啟用**——不錄音、不上傳、不多打任何 API；樣本只存在該場 session，結束即刪
- **任務庫**：狀態切換、列內編輯（名稱/負責人/期限）、**手動新增任務**、刪除、CSV 匯出
- **專門的講者分離**（選用，設 `PYANNOTE_API_KEY`）：用 pyannoteAI 聲學模型對整份音檔分群，再依時間戳重標 Gemini 逐字稿的講者——77 分鐘協商實測從 3 位分到 19 位；預錄聲音辨識人可選擇改用 voiceprint 比對（預設關）。沒設就照舊，失敗也自動退回（見「專門的講者分離」）
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
| 跨會議問答（RAG） | `app/rag.py` | Gemini 向量嵌入 + 語意檢索，跨所有會議回答提問；索引與會議／任務同一個 store（本地 JSON 或 Firestore） | — |
| 資料庫與任務分發 | `app/agents/executor_agent.py`、`app/stores/` | 本地 JSON（`data/output/db.json`）；填 Firebase 金鑰即自動改用 Firestore 雲端持久化 | ✅ Firestore 已接（`FirestoreStore` 實作同一 `TaskStore` 介面） |
| 時程同步與通知 | `app/agents/notifier_agent.py` | 產生信件草稿與事件 JSON 存本地 | 9 月串 Gmail / Google Calendar API |

### 轉錄後端可切換

`TRANSCRIBE_ENGINE` 決定轉錄怎麼做，兩者共用同一組介面（`transcribe / device / model_size`），靠 `create_app` 的依賴注入互換：

- `local`（預設）：本地 **faster-whisper**，需要 NVIDIA GPU，完全離線、不耗 API 額度——本機開發用這個
- `gemini`：把音訊丟給 **Gemini** 直接轉錄，不需要 GPU——雲端部署用這個（見下方「部署到雲端」）

### 用到的模型與免費額度

轉錄與分析是**兩個獨立的 Gemini 模型設定**，可各自用環境變數覆蓋：

免費層實測額度（2026/07 於 [AI Studio 儀表板](https://aistudio.google.com/rate-limit)確認，Google 可能調整，以你自己的儀表板為準）：

| 模型系列 | RPM | TPM | **RPD（每日）** |
|---|---|---|---|
| **Flash Lite**（本專案主力） | 15 | 250K | **500** |
| **Flash**（僅作備援） | 5 | 250K | **20** |

> 兩者差 25 倍——這是所有預設值的取捨依據：能用 Lite 解決的就不動用 Flash。

| 用途 | 環境變數 | 預設模型 | 免費額度（每專案每日）|
|------|----------|----------|----------------------|
| 音訊轉錄 | `TRANSCRIBE_MODEL` | `gemini-3.5-flash-lite` | 500 次/日、15 次/分 |
| 長音檔分段秒數 | `TRANSCRIBE_CHUNK_SECONDS` | `240`（0＝不分段） | 每段各算一次轉錄請求；**六分鐘以內的檔案不分段**，整份送出 |
| 標註率不足時的 Lite 重試次數 | `TRANSCRIBE_LABEL_RETRIES` | `2` | 每次僅佔每日額度 0.2% |
| 長檔／講者標註備援的強模型 | `TRANSCRIBE_FALLBACK_MODEL` | `gemini-3.5-flash`（空＝關閉） | 比 lite 分講者好，但每日僅 20 次且常 503 |
| 長檔改用強模型的門檻（秒）| `TRANSCRIBE_LONG_FILE_THRESHOLD_SECONDS` | `0`（停用，長檔一律 lite 分段）| 設 600 即恢復長檔用強模型 |
| 每個檔案最多幾段可用備援模型 | `TRANSCRIBE_MAX_FALLBACK_CHUNKS` | `0`（＝預設完全不動用 Flash） | 設 1 才會在重試仍失敗時降級 |
| 單一檔案的重試總上限 | `TRANSCRIBE_MAX_RETRY_CALLS` | `10` | 讓重試成本與影片長度脫鉤 |
| 分段之間往前多抓幾秒 | `TRANSCRIBE_OVERLAP_SECONDS` | `20` | 讓講者標籤跨段接得起來 |
| 聲紋接力最多記幾位講者 | `VOICE_RELAY_MAX_SPEAKERS` | `20`（設 0 停用） | 只影響長檔分段轉錄；每段重傳全部樣本，77 分鐘的檔約多花數分鐘。**設了 `PYANNOTE_API_KEY` 時自動跳過** |
| 會議分析、跨會議問答 | `GEMINI_MODEL` | `gemini-3.5-flash-lite` | 同上 |
| 錯字校正（選用） | `CORRECT_MODEL` | `gemini-3.5-flash-lite` | 同上 |
| 預錄聲音辨識人（選用） | `VOICE_MATCH_MODEL` | `gemini-3.5-flash` | 一場會議只打 1 次，用強模型換準確度 |
| 最多可註冊幾個人的聲音 | `LIVE_ENROLL_MAX_SPEAKERS` | `4`（0＝停用整個功能） | 未勾選就完全不產生請求 |
| 專門的講者分離（選用） | `PYANNOTE_API_KEY` | 空＝不啟用 | 非 Gemini 額度；pyannoteAI 試用 150 小時，見下方 |
| 講者分離模型 | `PYANNOTE_MODEL` | `precision-2` | 只有 precision-2 支援 voiceprint |
| 單一講者分離工作最多等幾秒 | `DIARIZE_TIMEOUT_SECONDS` | `900` | 等不到就退回 Gemini 代號 |
| 預錄聲音辨識人改用 voiceprint | `PYANNOTE_VOICEPRINT_ENABLED` | `0`（關） | 每預錄一人扣 1 個 voiceprint（試用僅 10 個）；關閉時姓名改由 Gemini 在重標後比對 |
| 聲紋比對門檻（0~100） | `VOICEPRINT_MATCH_THRESHOLD` | `50` | 實測本人 89、非本人 ≤28（僅在開啟 voiceprint 時有作用） |

> **為什麼分兩個設定**：即時聆聽每 45 秒轉錄一次，吃掉絕大多數請求；分析每場會議只呼叫 1~3 次。兩者拆開，就能各自挑模型、額度互不排擠。
>
> **關於 429（配額爆掉）**：免費層每日額度是**「每專案每模型」共用**——同一個 Google 專案下的多把 key 共用同一份額度，加 key 只增加每分鐘吞吐、不增加每日總量。要更高每日量：改用**不同專案**的 key，或升級付費。實際額度看 [AI Studio](https://aistudio.google.com/rate-limit)。
>
> **品質 vs 額度**：想要更好的分析品質，可把 `GEMINI_MODEL` 設為 `gemini-3.5-flash`（推理較強，但免費層每日僅 20 次，適合少量分析）。
>
> **多人會議的講者分辨**：轉錄的 prompt 已強力要求標註講者，但實測 `gemini-flash-lite` 對「誰在講話」的辨識仍不穩定，3 人以上時常被併成一兩位。需要準確標出多位講者時，把 `TRANSCRIBE_MODEL` 設為 `gemini-3.5-flash`（可正確分出多位講者，代價是免費每日額度較低）。**不要用任何 `-latest` 結尾的別名**（`gemini-flash-latest`、`gemini-flash-lite-latest`…）——那會飄到當下最新版，而剛發布的版本正在被全世界搶，回的就是 `503 UNAVAILABLE / This model is currently experiencing high demand`。2026-09 一支 10 分鐘的上傳整份失敗即是此因，之後所有預設模型都改成釘死版本。
>
> **講者標註失敗會自動重試**：實測同一段音訊、同一個模型、`temperature=0`，講者標註率可能是 20% 也可能是 100%——這是**執行間的變異**，不是音訊太難，也不是分段太長（縮短分段沒有改善）。所以某一段的標註率低於 **80%** 時會自動重跑。門檻訂在 0.8 而非 0.5：prompt 要求每一行都標講者，一半沒標就是模型沒照做。重跑仍失敗時可改用 `TRANSCRIBE_FALLBACK_MODEL` 跑那一段，但**預設 `TRANSCRIBE_MAX_FALLBACK_CHUNKS=0`，等於預設不啟用**——要開再設 1。**設了 `PYANNOTE_API_KEY` 時不為講者標籤重跑**：講者會依時間戳整份重標，Gemini 標得再差都用不到，重試只留給「整段放棄轉錄」（有時間戳且有內容的行低於 80%）。實測 10.5 分鐘質詢：3 段原本全因標註率低各重跑 2 次，9 次呼叫 114 秒 → 3 次 39 秒，重標後 58 行全部有講者。
>
> **為什麼重試次數多、降級次數少**：Flash Lite 每日 500 次、Flash 只有 20 次（差 25 倍）。既然失敗是執行間的變異，多試幾次的累積成功率就很划算——實測單次成功率約 6 成，試 3 次約 94%，只花掉 Lite 額度的 0.6%；而降級一次就吃掉 Flash 額度的 5%。所以預設是「Lite 重試 2 次，Flash 完全不用」（`TRANSCRIBE_MAX_FALLBACK_CHUNKS=0`）——把額度全花在便宜又有效的 Lite 重試上。
>
> **長音檔會自動分段轉錄**：實測把整份 17 分鐘的質詢錄音丟給 `gemini-flash-lite`，講者標註會**整份消失**、時間戳還會漂到比實際長度多 3 分鐘；同一支影片只取前 3 分鐘卻能正確分出講者A/B/C。模型在長音訊上顯然會放棄逐句標註，所以超過 6 分鐘的音檔會先用 ffmpeg 切成每段 `TRANSCRIBE_CHUNK_SECONDS`（預設 240 秒）再逐段轉錄，最後把時間戳平移回整場時間、講者標籤跨段沿用同一組。代價是每段各算一次 API 請求（17 分鐘的檔約 5 次）。設成 `0` 可關閉分段、回到整份送出的舊行為。
>
> **分段時講者跨段一致（聲紋接力，預設開）**：分段轉錄每段都是模型的全新呼叫，它沒有跨段記憶，純文字提示「請沿用同一個代號指稱同一個人」時模型其實從沒聽過那個人的聲音，等於要它憑空判斷——實務上常見同一位委員在不同段被標成不同代號。做法是每段轉完，替新出現的講者代號（挑該段內容最長的一句）剪一小截嗓音存進「聲音簿」，從下一段開始當參考音訊一起送給模型，讓「沿用代號」從文字指示變成真的聽得到、比對得了的依據。封頂 20 位（`VOICE_RELAY_MAX_SPEAKERS`，設 0 停用）。**只影響長檔分段轉錄**——即時聆聽每段是獨立的短檔，不經過這條路。代價是每段都要重傳全部樣本、重試也會重傳，一支 77 分鐘的檔約多 240~400 次上傳往返、多花數分鐘；趕時間或額度吃緊就設 0，退回純文字提示＋分段重疊（`TRANSCRIBE_OVERLAP_SECONDS`）接力。設了 `PYANNOTE_API_KEY` 時會自動跳過（見下一節）。

### 專門的講者分離（pyannoteAI，選用）

設了 `PYANNOTE_API_KEY`（且 `TRANSCRIBE_ENGINE=gemini`）就改成**混合式講者辨識**：專門的聲學模型 [pyannoteAI](https://www.pyannote.ai) Precision-2 負責「誰在何時講話」，Gemini 照舊負責把話轉成文字，兩者依時間戳對齊。沒設金鑰時一切照舊，講者由 Gemini 自己標。

**為什麼**：分段轉錄每段都是 Gemini 的全新呼叫、沒有跨段記憶，上面那些重疊提示、標註率重試、聲紋接力，本質上都是請 LLM 用聽的去猜同一個人。pyannote 對**整份音檔一次**分群，代號天生全場一致。用 77 分鐘《無人機特別條例》黨團協商實測（`eval/diarize_poc.py`）：

| | Gemini lite 分段標註 | pyannote 重標 |
|---|---|---|
| 分出幾位講者 | 3 位 | 19 位 |
| 有講者標籤的行 | 53% | 96% |
| 講者分離耗時 | — | 44 秒（上傳 18MB Opus 另約 24 秒） |

- Gemini「換人講話」那行的時間戳，距 pyannote 偵測到的開口時間中位數 0.3 秒、99% 在 2 秒內——分段內的時間戳夠準，才撐得起用時間戳對齊
- 對齊：每行歸給與「本行→下一行時間戳」**重疊秒數最多**的講者（行首那一瞬間常還是上一位的尾音）；時間戳超出音檔長度的行直接丟掉（實測全是休會靜音段的幻覺）；落在靜音、3 秒內也沒人開口的行只拿掉標籤
- **上傳檔案**：分群在轉錄開始前就送出、與轉錄並行；轉錄完若分群還沒好，進度顯示「辨識講者中…」
- **即時聆聽**：聆聽中照舊由 Gemini 標代號當預覽；按結束時把各段錄音串起來（略過段與段之間重疊的 3 秒）整場重標一次
- **預錄聲音辨識人**：**預設不使用 voiceprint**（`PYANNOTE_VOICEPRINT_ENABLED=0`）——結束時照樣用 pyannote 整場重標，之後把重標後的逐字稿切回各段錄音，交給 Gemini 聲紋比對，姓名直接對上 pyannote 的代號（聆聽中 Gemini 提早認出的名字掛在舊代號上，重標後會被這次結果取代）。實測同一段協商，與 voiceprint 認出的是同一個人，且不消耗 voiceprint。設成 1 才改用 pyannote voiceprint 比對。門檻 `VOICEPRINT_MATCH_THRESHOLD=50`：實測本人 89 分、不是本人 16~28 分；API 預設 0 時，片段裡根本沒出現的人被硬配給一位只講 4 秒的人
- 聲紋接力在這個模式下自動跳過（代號反正會被整份重標蓋掉）
- **任何失敗都退回 Gemini 的代號**：pyannote 當掉、額度用完、逾時、ffmpeg 壓不動，轉錄與分析照常完成
- 本地 Whisper 引擎不啟用：它的輸出沒有時間戳，對不回任何一行

**費用**：新帳號 30 天試用含 150 小時與 10 個 voiceprint（需公司或學校信箱註冊，Gmail 不行）；之後要訂閱（Developer 方案 €19/月起），沒訂閱 API 會回 402，系統就自動退回 Gemini 標註。voiceprint 按**建立次數**計費：開啟時預錄 4 個人＝用掉 4 個，所以預設關閉（姓名改由 Gemini 比對，每場多 1 次強模型請求）。

**隱私**：啟用後，會議音訊與預錄的聲音樣本會上傳到 pyannoteAI 的暫存區處理，官方說明 24~48 小時內自動刪除；本服務這一側照舊不保存。

**確認有沒有啟用**：打開 `/api/health`，`speaker_diarization` 是 `"pyannote"` 就是有；`null` 代表沒金鑰或引擎不是 gemini。

### 可靠性

- **多把金鑰輪替**：`GEMINI_API_KEYS`（逗號分隔）round-robin，每次呼叫換下一把；撞 429 自動跳下一把
- **503 過載自動退避重試**：Google 端暫時過載時指數退避（2s→5s→12s→30s→60s，各加 0~25% 亂數避免多個請求同時重試）重試最多 5 次，整個窗口約兩分鐘
- **長檔單段失敗不整份作廢**：分段轉錄時某一段重試用盡仍失敗，只在逐字稿留下「這段轉錄失敗」的缺漏標記並繼續轉下一段；全部段落都失敗才報錯
- **持續過載時自動換模型**：退避重試用盡仍是 503（「This model is currently experiencing high demand」），就改用另一個模型整輪重試——`gemini-3.5-flash-lite` ↔ `gemini-3.5-flash`，兩者負載與額度分開計算，一個過載另一個常常正常。涵蓋轉錄、分析、錯字校正、聲紋比對、翻譯、問答；只有過載才換（429 額度用完不換，免得把另一顆額度也吃掉）。代價是 lite 過載時會用到 3.5-flash 每日僅 20 次的額度
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

repo 已附 `Dockerfile`（含 ffmpeg）、`requirements-cloud.txt`（精簡依賴，不含 faster-whisper；實際安裝的是完整鎖定版本 `requirements-cloud.lock`）與 `render.yaml` 藍圖。以 [Render](https://render.com) 免費方案為例：

1. 到 Render → **New → Blueprint**，連上這個 GitHub repo，它會自動讀 `render.yaml`
2. 部署過程會要你填 `GEMINI_API_KEY`（金鑰只存在 Render 後台，不進 repo）；多把 key 就改設 `GEMINI_API_KEYS`
3. 等 Docker build 完成，就會拿到一個公開網址（如 `https://meeting-agent.onrender.com`）
4. （選填）要用專門的講者分離：在 Render 後台 **Environment** 新增 `PYANNOTE_API_KEY`，存檔後會自動重新部署；打開 `https://你的網址/api/health` 看到 `"speaker_diarization": "pyannote"` 就是生效了

`render.yaml` 已預設好雲端需要的環境變數（`TRANSCRIBE_ENGINE=gemini`、轉錄與分析模型皆為 `gemini-3.5-flash-lite`）；金鑰類（`GEMINI_API_KEY`、`FIREBASE_CREDENTIALS_JSON`）標記 `sync: false`，不進 repo、由你在 Render 後台填。`API_TOKEN` 例外：標記 `generateValue: true`，由 Render 自己產一串隨機值，所以**新部署一開始就是鎖上的**——要登入時到後台 Environment 分頁把值複製出來。

> 免費方案注意：閒置一段時間後容器會休眠，下次連線需等約 30 秒冷啟動；檔案系統是暫時性的（重啟後 `db.json` 會清空）。要**永久保存任務資料**，加設 `FIREBASE_CREDENTIALS_JSON` 環境變數（見下方）即可切成 Firestore——跨會議問答的向量索引也一起存進去，所以重新部署之後不必把所有會議重新向量化。

#### 加上 API 認證（部署後網址是公開的）

沒有任何認證的話，任何人只要知道你的 Render 網址，就能直接呼叫 `/api/backup` 下載全部會議紀錄、`/api/restore` 覆蓋資料庫，或刪除任意會議/任務——沒有密碼也沒有登入頁，而且**看起來完全正常**：服務活著、首頁打得開，沒有任何徵兆顯示門是開的。

所以有兩道防線：

- `render.yaml` 的 `API_TOKEN` 用 `generateValue: true`，新部署一開始就有一把隨機鑰匙
- 萬一兩種把關方式都沒設（例如刪掉了變數，或服務是舊藍圖建的），偵測到跑在 Render 上時**啟動會直接失敗**，訊息說明少了什麼。寧可服務起不來，也不要它開著門假裝正常

用共用鑰匙時：

1. 到 Render 環境變數把 `API_TOKEN` 的值複製出來（自動產生的；也可以自己改成別的隨機字串）
2. 所有 `/api/*` 端點（除了 `/api/health`、`/api/auth/config`）都會要求帶 `Authorization: Bearer <token>`
3. 開啟網頁時，前端第一次打 API 會收到 401，跳出輸入框貼上這串 token 即可（存在瀏覽器 `localStorage`，之後不用再輸入）

本機開發預設不設 `API_TOKEN`，不會要求登入，啟動守衛也不會被觸發（它只看 Render 自動設的 `RENDER=true`）。真的要開一個沒有門的公開站，設 `ALLOW_NO_AUTH=1` 明講。

#### 上傳大小上限

`/api/media` 的單檔上限預設 500MB，`/api/live/*/chunk` 的每段音訊也套同一個上限；超過回 `413` 並且不留下半截檔案。免費方案的暫時性磁碟只有幾百 MB，寫爆之後連 `db.json` 都存不進去、整個服務跟著停擺，所以這是硬性的門檻而非建議值。磁碟更小的方案設 `MAX_UPLOAD_MB` 往下調即可（2 小時的單聲道會議錄音約 60~120MB）。

#### 速率限制（公開部署預設開啟）

公開網址上任何能登入的人都能連打上傳、問答，把整個服務共用的 Gemini 每日額度燒光（啟用 pyannote 後更是按時數計費）。所以耗額度的端點**每位使用者**各有每分鐘／每天上限，超過回 `429` 並附 `Retry-After`，前端會顯示「操作太頻繁，請 N 秒後再試」或「今天的檔案上傳已達上限」。

| 操作 | 名稱 | 預設（每分鐘 / 每天） |
|---|---|---|
| 貼上文字分析、重新分析 | `analyze` | 10 / 60 |
| 檔案上傳轉錄 | `media` | 3 / 20 |
| 開始即時聆聽 | `live_start` | 3 / 20 |
| 即時聆聽每段錄音 | `live_chunk` | 20 / 600（每分鐘放寬：結束時要補送失敗的段） |
| 詢問會議 | `ask` | 10 / 100 |
| 摘要翻譯 | `translate` | 20 / 300 |

- 偵測到 Render（`RENDER=true`）自動開啟；本機開發預設關閉。`RATE_LIMIT_ENABLED=1/0` 可強制開關
- 覆寫個別上限：`RATE_LIMITS=media=5/30,ask=20/200`（0＝不限）。寫錯格式會直接啟動失敗，不會悄悄變成沒有節流
- 計數存在記憶體，重新部署會歸零——要擋的是連打與濫用，精確用量看儀表板
- 只用共用 `API_TOKEN`（沒開 Google 登入）時，所有人算同一個使用者，等於整個服務共用一份上限
- 目前狀態看 `/api/health` 的 `rate_limit`

#### （選填）Google 登入：每個人一份自己的資料

不開登入的話，這個網址是**單人模式**——所有人上傳的錄音、產生的任務、加的自訂詞彙都寫進同一份資料，彼此看得到。`API_TOKEN` 擋得住陌生人，但擋不住「拿到 token 的人互相看到對方的會議」，因為它是一把共用鑰匙，只分「進不進得來」，不分「你是誰」。

要給多個人用（老師、同學各自試），就開 Google 登入：

1. 到 [Firebase Console](https://console.firebase.google.com) 的專案 → **Authentication → 開始使用 → Sign-in method → 啟用 Google**
2. **專案設定 → 一般設定 → 你的應用程式**，沒有網頁應用程式就新增一個，記下 `apiKey`、`authDomain`、`projectId`
3. 在 Render 環境變數新增 `FIREBASE_WEB_API_KEY`、`FIREBASE_AUTH_DOMAIN`、`FIREBASE_PROJECT_ID`
4. **同時確認 `FIREBASE_CREDENTIALS_JSON` 也有設**（見下一節）——驗證 ID token 需要 service account 金鑰
5. **Authentication → Settings → 授權網域**加入你的 Render 網址，否則登入視窗會被擋下
6. 重新部署。開啟網頁會先看到登入畫面，登入後右上角「設定」裡會顯示帳號與登出

開啟後的行為：

- 會議、任務、自訂詞彙、跨會議問答**全部依帳號分開**，A 問問題不會檢索到 B 的會議
- 轉錄工作與即時聆聽 session 也綁帳號：知道別人的 id 也看不到內容
- 換裝置、換瀏覽器都是同一份資料，不必手動保管任何鑰匙
- 設了登入之後 `API_TOKEN` 就不再生效——共用鑰匙不該還能繞過帳號制

> 只設前三個、忘了 `FIREBASE_CREDENTIALS_JSON` 的話，服務會**啟動失敗並說明少了什麼**。這是刻意的：悄悄退回單人模式會讓人以為網站已經上鎖，實際上是全開的，而且完全沒有徵兆。

> **四個值必須全部來自同一個 Firebase 專案。** 混到兩個專案（多人協作時各自填各自的最容易發生）的話，服務也會**啟動失敗並指名是哪兩個對不上**。這種錯自己查極貴：登入畫面過得去、看起來登入成功，但進去之後每個 API 都失敗，而錯誤只說 `InvalidIdTokenError`——因為 token 由 A 專案簽發、後端拿 B 專案的身分驗簽。

#### （選填）加入 Google 行事曆

開了 Google 登入之後，分析結果的「行事曆事件」區塊會多一顆**加入 Google 行事曆**，把有期限的代辦一鍵寫進使用者自己的行事曆。之後的提醒由 Google 負責發（手機、電腦、手錶），本服務不需要排程器，也不需要一支能跨帳號讀資料的通知端點。

要讓它能用，得在 [Google Cloud Console](https://console.cloud.google.com/apis/credentials/consent)（選同一個專案）的 **OAuth 同意畫面 → 資料存取** 加入範圍 `https://www.googleapis.com/auth/calendar.events`，並啟用 **Google Calendar API**。

> 同意畫面還在「測試中」的話，只有被加進**測試使用者**清單的帳號授權得了，其他人按下去會看到 Google 擋下來的畫面。展演前記得把要示範的帳號加進去，或把應用程式發布為正式版。

- 行事曆權限**不會**併進登入流程，是按下按鈕當下才另外要的——登入畫面若多一句「存取你的 Google 日曆」，會嚇退根本用不到這功能的人
- 每筆事件帶一個由「會議 + 任務名稱」算出的固定 id，所以同一場會議按第二次不會變成兩份，重複的那幾筆會顯示「先前已加過」
- 沒有期限的代辦、以及「沒人認領 / 議而未決」那兩類提醒進不了行事曆（它們沒有日期），仍然只在網站的「主動提醒」頁看得到

#### （選填）用 Firestore 永久保存資料

不設定就是本地 JSON，Render 重啟會清空；設定後所有會議與代辦改存 Google Firestore，重新部署也不會遺失。

1. 到 [Firebase Console](https://console.firebase.google.com) 建專案 → **Firestore Database** 按 **建立資料庫**（正式或測試模式皆可，本服務用 Admin SDK 直連不受安全規則影響）
2. **專案設定 → 服務帳戶 → 產生新的私密金鑰**，下載一份 service account JSON
3. 在 Render 環境變數新增 `FIREBASE_CREDENTIALS_JSON`，把整份 JSON 內容貼進去（單行、含大括號即可）
4. 重新部署。啟動後 `GET /api/health` 的 `store_backend` 會顯示 `firestore` 代表已生效

> 本機開發若要連 Firestore，改設 `FIREBASE_CREDENTIALS_FILE=/path/to/service-account.json`（指向檔案路徑），並先 `pip install firebase-admin`。

## 使用方式

- **文字貼上**：把會議紀錄 / 群組對話貼進文字框（`data/samples/` 有三份中英夾雜的模擬紀錄可以直接試）
- **檔案上傳**：支援 mp3 / wav / m4a / mp4 / mov / mkv 等；影片自動抽聲音軌。轉錄採串流回報，進度條與逐字稿會**邊轉邊長出來**（進度取「模型已聽到第幾秒 ÷ 音檔長度」，不是動畫）
- **即時聆聽**：允許麥克風後開始，每 45 秒（可在 `.env` 調整）自動送出一段轉文字，逐字稿即時增長；按「結束會議」彙整全文分析。可指定要用哪一支麥克風——錄聲音樣本與正式聆聽一律共用這一支，同一個人用不同裝置錄的樣本，音色差距足以讓聲紋比對失效
- **會議日期**欄位是相對日期（「下週五」）的換算基準，預設今天
- **AI 校正錯字**（預設關閉）：勾選後在分析前多跑一次 AI，用上下文修掉語音辨識的同音錯字（「涵式」→「函式」）。改了哪些字會列在結果最下方，隨時可核對

分析結果會顯示：會議摘要、會議重點（帶時間節點，點擊可跳到逐字稿出處）、出席者、決議、代辦（負責人／期限／優先級／原文出處）、待確認事項（議而未決）、確認信草稿、行事曆事件。所有代辦同時寫入頁面底部的「資料庫」。

**確認信一鍵開信**：草稿右上角的「Gmail」／「預設信箱」會開啟已填好主旨與內文的撰寫視窗，收件人留空由你自己選（出席者是姓名，對不到 email）。內容太長塞不進網址時會改成複製全文到剪貼簿並提示你貼上——寧可多按一下，也不要寄出被截斷的信。

產出的檔案在 `data/output/`：
- `db.json` — 任務庫（會議 + 攤平的任務）
- `notifications/<meeting_id>/email_draft.txt` — 確認信草稿全文
- `notifications/<meeting_id>/calendar_events.json` — Google Calendar `events.insert` 可直接使用的事件格式

## 設計決策備忘

- **負責人不明的代辦**：`owner` 為 null 並自動列入待確認事項——不讓 LLM 硬猜，降低幻覺
- **每個代辦附 `source_quote`**（逐字稿原句），方便人工核對準確率
- **錯字校正回傳「修正清單」而非整份逐字稿**：讓模型重寫全文，它會順手刪贅字、拿掉時間標記與講者標籤，時間標記一掉會議重點的跳轉就壞了。改成只回 `{wrong, right}` 清單、在本地字串取代，並驗證行數與時間標記數量不變（不符就整批放棄）——輸出 token 也少很多，且使用者看得到改了什麼
- **JSON 驗證失敗自動重試**：把 Pydantic 錯誤訊息回饋給 Gemini，最多 3 次
- **系統不從對話內容猜講者姓名，交給使用者事後改名**：曾經有「辨識名稱」選項，讓模型讀整份逐字稿判斷誰是誰。實測立法院質詢（翁曉玲詢問王榮璋），模型拿講者自己說的「主席好」「謝謝王委員的提問」判定他就是主席、王委員，被批評的「陳菊」也被安成講者；提示講明、本地再驗依據是誰說的，lite 仍會犯（補上主席開場後 3 次有 1 次把主席本人對應成翁曉玲）。猜錯的名字比代號更糟，而且使用者不會發現，所以整條移除：轉錄與分析一律用「講者A/B/C」，分析的 `attendees` 照抄講者欄代號（被討論的第三人不列），`owner` 只在會議中講出姓名時寫姓名、否則寫承接的講者代號。姓名由使用者在結果頁或歷史會議點講者改名——改名動的是講者欄（`js/speakers.js` 的 `renameSpeakerInTranscript`，內文提到的「講者A」與「講者AB」都不受影響）、出席者、摘要（`renameInText`：英數字邊界防「講者AB」「Speaker 20」誤換、新名含舊名時不疊字、單字名不換）與負責人是該代號的任務
- **預錄的人當「已確認出席者」餵進分析，但不得用來猜負責人**：會前登記並錄了樣本＝使用者親自指認「這些人在場、姓名這樣寫」。模型從逐字稿的「小明」「王先生」還原不出完整姓名，`attendees` 因此常缺漏或寫法不一，這份名單一次解決兩件事。紅線與詞彙表相同：在場不等於負責——逐字稿裡沒有明確指派時，禁止把名單上的姓名填進 `owner` 或任何其他欄位，否則名單只有兩個人時，找不到負責人的代辦會被硬塞給其中一位，而且看起來像有憑有據
- **聲紋比對是獨立一步，不塞進轉錄**：把聲音樣本餵進轉錄、讓模型直接標姓名，會正面撞上「轉錄一律輸出代號」那條規則——`SPEAKER_RE` 只認代號格式，模型一吐姓名，`speaker_label_ratio` 就歸零並觸發無謂的重試，跨段講者一致性也跟著失效。而且即時聆聽每 45 秒轉錄一次，每次都重帶樣本，一小時會議多出約 10 萬 input token。改成**單獨打比對呼叫**：只送註冊樣本＋幾段代表性的會議錄音（每個代號挑它講最多話的那一段，見 `pick_evidence_chunks`），音訊量與會議長度脫鉤，一場約 220 秒。一場最多打兩次——收到第 2 段有講者標籤的逐字稿之後先在背景比一次（開完一小時才發現預錄沒生效已經來不及，提早比才來得及補救），結束時再比一次、與前一次的結果合併（後半場才發言的人補得進來，衝突時以證據較多的後一次為準，合併後同一個姓名不得落在兩個代號上）
- **預錄姓名直接套用，但一樣要過驗證**：預錄樣本的姓名是使用者自己登記的，是系統唯一會自動填的姓名。但比對結果只是「建議」——姓名仍須通過 `is_safe_name`、且改寫一律走 `apply_speaker_names`（驗行數、時間標記、重複姓名）。模型回傳沒註冊過的名字（從逐字稿內容猜的）會被直接丟掉：聲紋比對的依據只能是嗓音
- **聲音樣本不落地保存**：聲紋是生物特徵資料。樣本只存在該場 session 的暫存目錄，跟著錄音段在 `finish()` 一起刪除，也受閒置 TTL 回收——不進資料庫，省掉一整類隱私與跨帳號外洩的問題。啟用 pyannote 時會議錄音會上傳到 pyannoteAI 暫存區處理（24~48 小時內自動刪除）；聲音樣本只有在開啟 voiceprint 時才會上傳給 pyannoteAI，voiceprint 也只在該次比對中使用、不另外保存
- **講者用專門模型、文字用 Gemini（混合式）**：專門的 STT API 大多一次做完轉錄＋分講者、字級時間戳對齊最準，但台語質詢與詞彙表是 Gemini 的強項，整套換掉風險太高。所以只把「誰在何時講話」交給 pyannote，再用時間戳接回 Gemini 的逐字稿；PoC 先量過 Gemini 分段內的時間戳誤差（中位數 0.3 秒）才定案。pyannote 是加分項：沒金鑰、失敗、逾時一律退回 Gemini 的代號
- **比對結果要快取，否則「重試分析」會把姓名整組賠掉**：`finish()` 刪掉音檔之後就沒有聲音可比了，而第一次分析失敗（額度、網路）按重試會再走一次同樣的路徑。比對結果因此存在 session 上，重試時直接沿用；檔案已不存在時一律回快取，不再白打一次 API
- **聲紋跨段接力（分段轉錄自動啟用）與上面的聲紋比對是兩套不同機制，沒有牴觸**：聲紋比對是會議結束後單獨打一次、把代號換成真實姓名；跨段接力是分段轉錄**進行中**，把已確立代號的聲音樣本當參考音訊接力餵給下一段，目的只是讓模型**沿用同一個代號**，並不要求輸出姓名——所以不會違反「轉錄一律用代號」那條規則，`speaker_label_ratio`／`SPEAKER_RE` 都不受影響。動機：分段轉錄每段都是全新呼叫，模型沒有跨段記憶，純文字提示「請沿用同一個代號」時它其實沒聽過那個人的聲音，等於憑空判斷；接力餵樣本後，「沿用代號」從文字指示變成真的聽得到、比對得了的依據。樣本剪取失敗只讓那個代號退回純文字提示，不影響整份轉錄
- **即時聆聽的分段策略**：每段用新的 MediaRecorder 錄（而非 `timeslice`），確保每段音訊都有完整檔頭、可獨立解碼。相鄰兩段刻意重疊 3 秒——下一段的錄音器提早 3 秒開始錄，一句話才不會被 45 秒的硬切點剁成兩半（`stop` 與 `start` 之間還有幾十毫秒是真的沒錄到）；重疊那幾秒會被轉錄兩次，後端依絕對時間濾掉（`drop_lines_before`，與長檔分段同一套）。每段的提示也改帶前一段結尾的逐字稿（`chunk_hint`）：只給講者清單，模型沒聽過前一段，無從知道「講者B」是哪個嗓音，只能從自己這段重新編號
- **聆聽 session 遺失時用瀏覽器的逐字稿副本救援**：session 只存在伺服器記憶體，行程一重啟（雲端重新部署、當掉重生）就永遠找不回來，`finish` 會一直回 404，重試幾次都一樣。所以前端每收到一段轉錄就留一份逐字稿副本，`finish` 撞到 404 時改走「貼上文字」既有的 `POST /api/meetings` 把整場救回來。重啟後送出的錄音段也會一起失敗，救回來的逐字稿可能缺後半段，因此會明白提示使用者核對結尾
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
│   ├── gemini_transcriber.py # 雲端 Gemini 轉錄（時間戳 prompt、長音檔自動分段、聲紋接力）
│   ├── pyannote_client.py    # pyannoteAI REST 客戶端：上傳、分群、voiceprint、identify、輪詢
│   ├── diarizer.py           # 講者分離接進上傳轉錄（並行）與即時聆聽（結束時整場重標）
│   ├── speaker_align.py      # 依時間戳把 pyannote 講者填回逐字稿每一行
│   ├── live_session.py       # 即時聆聽 session：並發配位、逐段累積、聲音樣本註冊
│   ├── voice_match.py        # 預錄聲音辨識人（選用）：聲紋比對代號→姓名
│   ├── speaker_names.py      # 預錄姓名套用到講者欄：姓名驗證、本地改寫並驗證行數與時間標記
│   ├── segments.py           # 分段結果縫合：時間戳平移、跨段講者一致性、比對證據挑選
│   └── media.py              # ffmpeg 抽音軌、取長度、切段、壓 Opus、串接即時聆聽錄音段
├── stores/               # TaskStore 介面 + 本地 JSON / Firestore 兩種實作
├── jobs.py               # 音檔/影片背景轉錄工作佇列
├── rag.py                # 跨會議問答（向量嵌入 + 語意檢索 + AskAgent）
├── translate.py          # 即時翻譯（逐段/摘要）
├── glossary.py           # 自訂詞彙表（轉錄與分析 prompt 共用）
├── export.py             # 匯出：任務 CSV、行事曆 .ics、Markdown 會議報告
├── usage.py              # 今日 API 用量統計
├── ratelimit.py          # 速率限制：每位使用者每種操作的每分鐘／每天上限（429）
├── gemini_keys.py        # 多金鑰輪替 + 429/503 重試
├── timeutil.py           # 本地時區（UTC+8）工具
├── atomicio.py           # 原子寫檔（斷電不壞資料）
└── evaluation.py         # 任務抽取 precision/recall（供 eval/run.py）
eval/                     # 量化評估：標注資料集 + 評估腳本；diarize_poc.py 講者分離實測
tests/                    # pytest 測試（865，全部離線、不需金鑰；前端講者判斷另需 node）
Dockerfile                # 雲端部署映像（Python + ffmpeg，轉錄用 Gemini）
requirements-cloud.lock   # 雲端完整鎖定版本（Dockerfile 與 CI 共用）
ruff.toml                 # lint 規則（CI 跑 ruff check .）
render.yaml               # Render 一鍵部署藍圖
data/samples/             # 模擬會議紀錄（中英夾雜、含邊界案例）
data/output/              # 任務庫與通知產出
```

### 前端（app/static/，ES modules、無建置步驟）

```
app/static/
├── index.html            # 畫面結構（每個 view 有註解標示）
├── style.css             # 樣式（檔頭有分區目錄）
├── js/                   # 行為，進入點 js/main.js（檔頭列出各模組職責）
├── orb.js                # 即時聆聽的音量球（獨立載入，不碰麥克風）
├── icons.svg             # Lucide 圖示雪碧圖（<use> 引用）
├── icon.svg / manifest.webmanifest / sw.js   # PWA
```

`js/` 各模組（原本單一 `app.js` 拆出來的，載入順序見 `main.js`）：

| 模組 | 內容 |
| --- | --- |
| `api.js` | 全站唯一知道端點網址與請求形狀的地方 |
| `core.js` | 共用基礎：`$`、esc、SVG 圖示、錯誤橫幅、骨架、分頁、API 認證 |
| `auth.js` | Google 登入（伺服器有設 Firebase 才作用） |
| `router.js` | 版面路由：側欄一次只顯示一個 view（`showView`） |
| `setup.js` | 全域初始化：會議日期、會議種類、功能勾選、本次專用詞彙、麥克風選擇 |
| `speakers.js` | 行首「XXX：」算不算講者：代號一律算、像名字的出現兩次以上或在出席名單上才算；講者改名（講者欄、摘要文字）與新名字驗證（純函式，`tests/test_speaker_labels_js.py` 用 node 測） |
| `transcript.js` | 逐字稿渲染（時間欄＋講者＋內文）、時間/引用句跳轉 |
| `result.js` | 分析結果：摘要、會議重點、決議、代辦、確認信、種類專屬區塊 |
| `calendar.js` | 代辦寫進 Google 行事曆 |
| `tasks.js` | 任務庫：清單、搜尋篩選、列內編輯、手動新增 |
| `meetings.js` | 歷史會議：查閱、編輯、重新分析、分享、講者改名、刪除 |
| `reminders.js` | 主動提醒：到期掃描與每日通知 |
| `home.js` | 首頁儀表板、今日用量 |
| `ask.js` | 跨會議問答（RAG）＋關鍵字即時搜尋 |
| `inputs.js` | 三條輸入路徑：文字貼上、檔案上傳（含拖曳）、即時聆聽、預錄聲音辨識人 |
| `settings.js` | 主題、設定選單、自訂詞彙、備份還原、PWA |
