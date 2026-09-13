# 講者辨識改用 pyannoteAI（混合式）實作計畫

日期：2026-09-14　狀態：待批准

## 決策紀錄

| 問題 | 定案 |
|---|---|
| 方案 | **混合式**：pyannoteAI Precision-2 負責「誰在何時講話」，Gemini 照舊轉文字，依時間戳對齊 |
| 範圍 | 上傳檔案轉錄、預錄聲音辨識人、即時聆聽 |
| 舊機制 | 保留 Gemini 標註當**無金鑰／API 失敗退路**。~~聲紋跨段接力程式碼刪除~~ → **作廢**：遠端 `e3c796c` 已正式啟用接力，使用者指示不更動 git 上的新功能，一律共存 |
| 即時聆聽 | 聆聽中照舊 Gemini 標代號當預覽；**結束時整場接起來跑一次** pyannote 重標 |
| 預錄樣本 | 正規 **voiceprint + identify**（有信心分數與門檻） |

### 為什麼

- 分段轉錄的講者代號跨段不一致，是「每段都是模型的全新呼叫」造成的結構性問題；
  pyannote 對**整份音檔一次**分群，代號天生全場一致，不必再靠文字提示／聲紋接力硬接。
- Gemini 轉文字保留：台語、繁中、詞彙表的品質是現有強項，專門 STT API 在台語上風險高。
- identify 的輸出同時帶 diarization 與 `{SPEAKER_xx → 姓名, confidence}`，
  正好接上既有的 `speaker_prior` 管線（`orchestrator.process_transcript`）。

## API 契約（取自 https://docs.pyannote.ai/openapi.json）

- Base `https://api.pyannote.ai`，Header `Authorization: Bearer <key>`
- 上傳：`POST /v1/media/input {"url": "media://<key>"}` → `{"url": presigned}`；
  `PUT presigned`（`Content-Type: application/octet-stream`）。暫存 24~48h 自動刪
- `POST /v1/diarize {url, model:"precision-2", exclusive:true, min/maxSpeakers?}` → `{jobId, status}`
- `POST /v1/voiceprint {url}` → job；output `{voiceprint: base64}`（**按建立次數計費**，試用 10 個）
- `POST /v1/identify {url, voiceprints:[{label, voiceprint}], matching:{threshold, exclusive}, exclusive:true}`
  → output `{diarization[], exclusiveDiarization[], identification[], voiceprints:[{speaker, match, confidence:{label:score}}]}`
- `GET /v1/jobs/{id}` → `{status: pending|created|running|succeeded|failed|canceled, output}`，output 保留 24h
- 速率：送 job 100/min、查 job 300/min；429 帶 `Retry-After`
- 段落：`{speaker:"SPEAKER_00", start: 秒(float), end: 秒}`

## 架構

```
上傳檔案（jobs.py）
  抽音軌 ─┬─ diarizer.submit(audio)          ← 先送出，與轉錄並行
          └─ transcriber.transcribe(audio)   ← Gemini 照舊（含講者代號，當退路）
          → diarizer.wait(job) → speaker_align.relabel(transcript, segments)
          → 失敗/逾時/無金鑰：沿用 Gemini 代號，log warning

即時聆聽（live_session.py，finish 前）
  各段 chunk 轉 wav 串接（記錄每段在串接檔的起點 ↔ 前端給的 offset）
  有樣本：每人 voiceprint（並行）→ identify(串接檔) → segments + {SPEAKER_xx: 姓名}
  無樣本：diarize(串接檔) → segments
  → 段落時間映射回整場時間 → relabel 整份逐字稿 → speaker_prior={講者X: 姓名}
  → 無金鑰：退回既有 VoiceMatcher（Gemini）
```

### 對齊規則（`speaker_align.py`，純函式，核心測試對象）

1. 代號映射：pyannote 講者依**首次出現時間**排序 → 講者A、B、C…（沿用 `SPEAKER_RE` 格式，下游不用改）
2. 每一行有時間戳的行，區間＝`[本行時間, 下一個時間戳)`；最後一行＝`[t, min(t+15, 音檔長度)]`
3. 取 `exclusiveDiarization` 中與該區間**重疊秒數最多**的講者（不是只看起點：Gemini 時間戳只到秒且會漂）
4. 時間戳 > 音檔長度 → **整行丟掉**（PoC：這類行 100% 是靜音段幻覺）
5. 零重疊（落在靜音）→ 找 ±`ALIGN_TOLERANCE_SECONDS`（預設 3）內最近的段；再沒有 → 移除該行講者標籤（前端續行沿用上一位）
6. 沒時間戳的續行不動；逐字稿完全沒時間戳（Whisper 引擎）→ 原樣回傳、不送 diarize
7. `relabel` 回傳 `(新逐字稿, stats)`，stats 含對上/容差對上/未對上的行數，寫進 log 供實測調參

## 任務分解（TDD：每項先寫失敗測試）

### Phase 0 — PoC 實測（不動主程式）✅ 2026-09-14 完成，**Go**
- [x] 0.1 `eval/diarize_poc.py`：用 1:17:03《無人機特別條例》黨團協商跑 diarize
- [x] 0.2 同檔以正式流程重跑 Gemini lite 分段轉錄（457s、410 行）＋離線對齊
- [x] 0.3 格式／耗時／段落數／未對上比例

**實測數據**

| 項目 | 結果 |
|---|---|
| 上傳 | FLAC 16k mono s16 107.7MB，**上傳 189s**（瓶頸） |
| pyannote 處理 | **44s**，770 段、**19 位講者**、語音 43.9 分；休會 46:30–68:00 語音 0s |
| Gemini lite 原本標註 | 只有講者A/B/C **3 個代號**，標註率 **53%** |
| 對齊：重疊命中 | 395 行 **96.3%** |
| 對齊：容差命中 / 未對上 | 1 行 0.2% / 14 行 3.4% |
| 未對上的 14 行 | **全部是幻覺**：休會靜音段模型重複吐同幾句、時間戳 `[1:32:39]`~`[5:24:40]` 超出音檔長度 |
| 換人行的時間戳準度 | 距 pyannote 發言起點 **中位數 0.3s、p90 0.8s、99% ≤ 2s** |
| 邊界可疑（首位佔比 < 60%） | 7 行 1.7% |
| 內容交叉驗證 | 主席「蔡總召已先發言…請王委員」→ 講者E 講民進黨立場、講者J 講民眾黨版本、「許書記長請」→ 講者K。Gemini 把蔡總召併進主席的講者A |

**據此調整設計**
- 時間戳 **超出音檔長度的行直接丟掉**（確定是幻覺）；音檔內但無語音的行只拿掉講者標籤（保守）
- 上傳是主要耗時 → Phase 2 加一項：試 Opus 低碼率（預估 ~15MB）確認分群結果不變再採用
- Gemini 時間戳在分段內很準，對齊規則（最大重疊、容差 3s）不需調整

### Phase 1 — 對齊核心（純函式，無網路）✅ 22 測試綠；PoC 資料重跑結果與原型一致
- [x] 1.1 `tests/test_speaker_align.py`：代號映射依首次出現排序
- [x] 1.2 最大重疊歸屬；一行跨兩位講者取多數
- [x] 1.3 零重疊容差、超出容差移除標籤、續行不動、無時間戳原樣
- [x] 1.4 最後一行區間上限、時間戳超出音檔長度
- [x] 1.5 identify 結果 → `speaker_prior`（經代號映射、過濾空 match）
- [x] 1.6 即時聆聽：串接檔時間 → 各段 offset 的映射（含段與段之間有空檔）
- 檔案：`app/transcription/speaker_align.py`

### Phase 2 — pyannote 客戶端 ✅ 客戶端 20 測試＋media 3 測試綠；真 API 冒煙測試通過（3 分鐘片段 11s、5 位講者）
- [x] 2.1 `tests/test_pyannote_client.py`（`httpx.MockTransport`）：上傳兩步驟、API 帶 Bearer、**預簽 PUT 不帶**、串流帶 Content-Length
- [x] 2.2 submit diarize / voiceprint / identify 的請求 body
- [x] 2.3 輪詢：running→succeeded 回 output；failed/canceled 丟錯；逾時丟錯；429 依 Retry-After（上限 60s、最多 5 次）；輪詢遇 5xx 重試（送出工作不重試，免重複計費）
- [x] 2.4 `media.encode_for_diarization()`：**Opus 32kbps**
- [x] `requirements-cloud.txt` 加 `httpx>=0.27`（`requirements.txt` 原本就有）
- 檔案：`app/transcription/pyannote_client.py`、`app/transcription/media.py`

**Opus vs FLAC 實測（同一支 77 分鐘檔）**

| | FLAC 16k | Opus 32kbps |
|---|---|---|
| 大小 / 上傳 | 107.7MB / 189s | **17.7MB / 24s** |
| 講者數 / 段數 | 19 / 770 | 18 / 694 |
| 逐 0.1 秒講者一致率 | — | **99.6%**（只有一位總共講 5.5 秒的人被併掉） |
| 逐字稿逐行標註一致率 | — | **98.7%** |

### Phase 3 — 上傳檔案流程 ✅ 21 新測試綠（全套 766）；真 Gemini＋真 pyannote 端到端 27s、19/19 行命中
- [x] 3.1 `tests/test_jobs.py`：有 diarizer 時逐字稿被重標再送分析；diarizer 本身 bug 時保留 Gemini 代號、job 仍 done；逐字稿為空不重標
- [x] 3.2 `app/transcription/diarizer.py`：`start()` 壓縮＋上傳＋分群丟背景（轉錄前送出、並行）、`apply()` 任何失敗原樣回傳；暫存 ogg 必刪；音檔長度取原檔
- [x] 3.3 job status `diarizing`（僅在轉錄完而分群還沒好時出現）；`inputs.js` `JOB_STATUS_ZH` 補「辨識講者中…」
- [x] 3.4 用量 `usage.record("diarize")`（每送一次分群工作記一次）
- [x] `build_diarizer(settings)`：有 `PYANNOTE_API_KEY` **且** `TRANSCRIBE_ENGINE=gemini` 才建（Whisper 沒時間戳）
- [x] `create_app(diarizer=)` 注入點＋API 接線測試；`conftest.py` 清掉 `PYANNOTE_API_KEY`（免得測試打真 API 燒額度）
- [x] 設定：`PYANNOTE_API_KEY`（空白視同未設）、`PYANNOTE_MODEL=precision-2`、`DIARIZE_TIMEOUT_SECONDS=900`
- 沒金鑰時：`diarizer=None`，`jobs.py` 流程與改動前完全相同

### Phase 4 — 即時聆聽＋預錄聲音辨識人
> 2026-09-14 同步遠端 `1c5f0f9` 後的前提變化（不更動其行為，只在其上加 pyannote 路徑）：
> - 即時分段現在**相鄰兩段重疊 3 秒**（`add_chunk(overlap_seconds=)`）→ 串接音檔時第二段起要**裁掉開頭重疊**，
>   否則同一段聲音出現兩次；placements 以「offset＋overlap」為每段實際起點
> - 預錄比對現在**第 2 段就先在背景比一次**（Gemini `VoiceMatcher`）並快取在 `session.voice_result`、
>   finish 時再比一次合併 → 聆聽中的提早回饋照舊走它；pyannote identify 只在 finish 做，
>   結果**覆蓋**快取中同代號的姓名（代號已被 pyannote 重編，舊快取的代號對不上新逐字稿），並寫回 `voice_result` 讓「重試分析」沿用
- [x] 4.1 `LiveSession.timing` 記錄每段 (offset, overlap)；`diarized_transcript` 快取重標結果
- [x] 4.2 `LiveSessionManager.diarize_session()`：組 pieces（略過重疊、起點＝offset＋overlap）→ `Diarizer.relabel_session()`
  （`media.concat_for_diarization` 串接→有樣本 voiceprint＋identify／無樣本 diarize→`to_session_time`→`relabel`）
- [x] 4.3 某人 voiceprint 失敗只略過那人；全部失敗退回純 diarize；純 diarize 不產生姓名
- [x] 4.4 快取：「重試分析」沿用重標逐字稿與姓名、不重打 API；重標後 Gemini 背景比對不得再寫回舊代號
- [x] 4.5 `live_finish`：先 `diarize_session`，回 None 才走既有 `voice_mapping`；`finish()` 回傳重標版
- [x] 聲紋門檻 `VOICEPRINT_MATCH_THRESHOLD=50`（實測：本人 89、非本人 16~28；門檻 0 時缺席者被誤配）
- [x] 沒有 offset（舊前端）、沒有語音、錄音段都不在 → 回 None 照舊
- 測試：diarizer 8＋live_session 10＋media 5＋client 1＋config 2＋API 接線 1，全套 793 綠
- **真 API 端到端**：3 分鐘片段切 4 段 45s webm（重疊 3s）＋預錄王委員 → Gemini 逐段轉錄大多沒標講者，
  pyannote 整場重標 **43/43 行命中**、**認出講者D＝王委員（內容確為民眾黨發言，正確）**，25 秒、2 個 pyannote 工作
- 試用 voiceprint 已用 3/10

### Phase 5 — 清理與設定
- [ ] 5.1 Gemini 端：有 diarizer 時 `label_retries=0`、`max_retry_calls=0`（講者交給 pyannote，不再為標註率燒額度）
- [ ] 5.2 ~~刪除聲紋跨段接力~~ 作廢（遠端已啟用，保留不動）。**使用者 2026-09-14 同意**：有 pyannote 金鑰時把 `voice_relay_max_speakers` 視為 0（接力結果反正會被重標覆蓋，卻多花 240~400 次上傳）；沒金鑰時行為完全不變
- [ ] 5.3 `render.yaml` 加 `PYANNOTE_API_KEY`（sync: false，Render 後台手動填）；`VOICEPRINT_MATCH_THRESHOLD`（Phase 4 定）。（其餘設定已在 Phase 3 加）
- [ ] 5.4 `.env.example`、README（引擎說明、費用、測試數）
- [ ] 5.5 全套 `pytest` 綠、README 測試數對齊

### 延後（本次不做）
- voiceprint 依（使用者, 姓名）快取進講者名冊，重複與會者不再重複計費
- Whisper 本地引擎：目前輸出沒有時間戳，要接 diarization 得先改輸出格式
- pyannote 串流 WebSocket 即時標註

## 風險

| 風險 | 緩解 |
|---|---|
| Gemini 時間戳漂移導致歸錯人 | 最大重疊＋容差；Phase 0 先量化；分段轉錄本就讓漂移限於單段內 |
| 大檔上傳慢（77 分 16k PCM ≈ 147MB） | 上傳前轉 FLAC／低碼率；與轉錄並行 |
| voiceprint 試用只有 10 個 | 延後項的名冊快取；README 註明 |
| pyannote 當掉／額度用完 | 一律降級回 Gemini 代號，不讓轉錄失敗 |
| 資料外送第三方 | README 註明音訊會上傳 pyannoteAI（暫存 24~48h 自動刪除） |
