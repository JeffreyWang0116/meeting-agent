/* ==================================================================
   把代辦事項寫進使用者自己的 Google 行事曆
   ------------------------------------------------------------------
   為什麼提醒走行事曆而不是自己排程寄信：事件一旦寫進去，之後的提醒
   就是 Google 原生行為——手機、電腦、手錶都會響，我們不必養一個排程器，
   也不必開一支「跨所有使用者讀資料」的端點去產生通知。

   權杖從 auth.js 現拿（見那裡的說明），這支只負責呼叫 API 與回報結果。
   沒啟用 Google 登入的部署（單人模式）拿不到權杖，按鈕整個不顯示。
   ================================================================== */
import { googleSignedIn, requestCalendarToken } from "./auth.js";
import { $, icon, nativeFetch, showError, showNotice } from "./core.js";

const ENDPOINT = "https://www.googleapis.com/calendar/v3/calendars/primary/events";

let events = [];
let token = null;          // 只放在記憶體：這是憑證，不進 localStorage

function setCalendarEvents(next) {
  events = Array.isArray(next) ? next : [];
  const btn = $("btnGCal");
  btn.hidden = !events.length || !googleSignedIn();
  btn.disabled = false;
  $("gcalStatus").textContent = "";
}

async function insertOne(event) {
  // 舊會議存下來的事件沒有 id 欄位（那是後來才加的）。沒有 id 就讓 Google
  // 自己配一個——代價是那些會議重複按會產生重複事件，但至少加得進去。
  const body = event.id ? event : { ...event, id: undefined };
  const resp = await nativeFetch(ENDPOINT, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return resp.status;
}

async function addAll() {
  let added = 0;
  let existing = 0;
  const failed = [];

  for (const event of events) {
    let status = await insertOne(event);
    if (status === 401) {          // 權杖過期（約一小時），重拿一次再試
      token = await requestCalendarToken();
      if (!token) throw new Error("授權已失效，請重新授權");
      status = await insertOne(event);
    }
    if (status === 409) existing += 1;        // 同 id 已存在＝先前加過了
    else if (status >= 200 && status < 300) added += 1;
    else failed.push(`${event.summary}（HTTP ${status}）`);
  }
  return { added, existing, failed };
}

function describe({ added, existing, failed }) {
  const parts = [];
  if (added) parts.push(`新增 ${added} 筆`);
  if (existing) parts.push(`${existing} 筆先前已加過`);
  if (failed.length) parts.push(`${failed.length} 筆失敗`);
  return parts.join("、") || "沒有可加入的事件";
}

$("btnGCal").addEventListener("click", async () => {
  const btn = $("btnGCal");
  const status = $("gcalStatus");
  btn.disabled = true;
  status.textContent = "授權中…";
  try {
    if (!token) token = await requestCalendarToken();
    if (!token) throw new Error("沒有取得授權");
    status.textContent = `寫入中…（共 ${events.length} 筆）`;
    const result = await addAll();
    status.textContent = describe(result);
    if (result.failed.length) showError(`部分事件未能加入：${result.failed.join("、")}`);
    else showNotice(`已加入 Google 行事曆：${describe(result)}`);
  } catch (err) {
    // 使用者自己關掉授權視窗不算錯誤，不必嚇他
    if (err.code === "auth/popup-closed-by-user" || err.code === "auth/cancelled-popup-request") {
      status.textContent = "";
      return;
    }
    token = null;
    status.textContent = "";
    showError(err.code === "auth/user-mismatch"
      ? "授權視窗裡選到了另一個 Google 帳號，請選擇目前登入的帳號。"
      : `加入行事曆失敗：${err.message}`);
  } finally {
    btn.disabled = false;
  }
});

// icon 只是為了讓按鈕圖示與其他按鈕一致，模組載入時填一次就好
$("btnGCal").innerHTML = `${icon("circle-plus", "i-sm")}加入 Google 行事曆`;

export { setCalendarEvents };
