/*
  會議助手 — 前端進入點（ES modules，無建置步驟）

  原本 2373 行的 app.js 拆成以下模組，載入順序＝原檔案的執行順序，
  行為完全不變。模組之間允許循環相依（例如 tasks ↔ home），
  因為彼此只在函式執行時才互相呼叫，不在模組求值階段就用到。

    api         全站唯一知道端點網址的地方（換平台時只要重寫這支）
    core        共用基礎：$ / esc / icon / 錯誤橫幅 / 骨架 / 分頁 / API 認證
    auth        Google 登入（伺服器有設 Firebase 才作用）
    router      版面路由：側欄一次只顯示一個 view
    setup       全域初始化：日期、會議種類、功能勾選、本次專用詞彙
    speakers    行首「XXX：」算不算講者（純函式，node 可直接測）
    audioguide  收音防護：依平台給說明、新裝置偵測、系統音源靜音（純函式）
    groupinvite 群組角色名稱、邀請信 Gmail 草稿網址（純函式）
    groups      工作區切換（個人／群組）、群組管理與邀請
    transcript  逐字稿渲染與時間/引用句跳轉
    progress    轉錄進度的剩餘時間推算（純函式）
    notify      瀏覽器通知（手機要經由 service worker 發）
    transcriptedit 逐字稿就地編輯的純函式（改句、改講者、合併、拆句、刪句）
    transcriptui 逐字稿就地修正的介面（接上面的純函式到畫面）
    result      分析結果頁
    refine      請 AI 修改結果（一句話 → 預覽 → 套用）
    tasks       任務庫
    meetings    歷史會議
    reminders   主動提醒
    home        首頁儀表板
    ask         跨會議問答
    prep        會議前準備：議程、會議系列、帶入上次沒做完的
    inputs      三條輸入路徑：文字貼上／檔案上傳／即時聆聽
    settings    主題、設定選單、詞彙表、備份、PWA
    push        推播的純函式（支援判斷、公鑰轉換）
    pushui      手機推播提醒的開關（訂閱／退訂／試送）
    templates   自訂會議範本（種類名稱、要哪些區塊）
    commentsui  對摘要／決議／代辦留言討論
    trashui     垃圾桶（30 天可救回）與群組活動紀錄
    insights    跨會議追蹤：未決事項、成員概況、週報月報
    gdocs       把會議紀錄存成 Google 文件（上傳 .docx 讓雲端硬碟轉檔）
*/
import "./core.js";
import "./auth.js";
import "./api.js";
import "./router.js";
import "./setup.js";
import "./speakers.js";
import "./audioguide.js";
import "./groupinvite.js";
import "./transcript.js";
import "./progress.js";
import "./notify.js";
import "./transcriptedit.js";
import "./transcriptui.js";
import "./calendar.js";
import "./refine.js";
import "./result.js";
import "./tasks.js";
import "./meetings.js";
import "./reminders.js";
import "./home.js";
import "./ask.js";
import "./prep.js";
import "./inputs.js";
import "./settings.js";
import "./push.js";
import "./pushui.js";
import "./templates.js";
import "./commentsui.js";
import "./trashui.js";
import "./insights.js";
import "./gdocs.js";
import "./groups.js";
