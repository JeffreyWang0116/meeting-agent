/*
  會議助手 — 前端進入點（ES modules，無建置步驟）

  原本 2373 行的 app.js 拆成以下模組，載入順序＝原檔案的執行順序，
  行為完全不變。模組之間允許循環相依（例如 tasks ↔ home），
  因為彼此只在函式執行時才互相呼叫，不在模組求值階段就用到。

    core        共用基礎：$ / esc / icon / 錯誤橫幅 / 骨架 / 分頁 / API 認證
    router      版面路由：側欄一次只顯示一個 view
    setup       全域初始化：日期、會議種類、功能勾選、本次專用詞彙
    transcript  逐字稿渲染與時間/引用句跳轉
    result      分析結果頁
    tasks       任務庫
    meetings    歷史會議
    reminders   主動提醒
    home        首頁儀表板
    ask         跨會議問答
    inputs      三條輸入路徑：文字貼上／檔案上傳／即時聆聽
    settings    主題、設定選單、詞彙表、講者名冊、備份、PWA
*/
import "./core.js";
import "./router.js";
import "./setup.js";
import "./transcript.js";
import "./result.js";
import "./tasks.js";
import "./meetings.js";
import "./reminders.js";
import "./home.js";
import "./ask.js";
import "./inputs.js";
import "./settings.js";
