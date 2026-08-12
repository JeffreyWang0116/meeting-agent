import { $, esc, icon, jsonOrThrow, loadFail, paginate, registerPager, registerRefresher, renderPager, showError } from "./core.js";
import { renderHome } from "./home.js";

let remindersLoaded = false;

/* ==================================================================
   7. 主動提醒：到期掃描與每日通知
   ================================================================== */
// ---- 主動提醒（Agent 自主掃描） ----
const ALERT_LABEL = {
  overdue: r => `逾期 ${r.days} 天`,
  due_soon: r => (r.days === 0 ? "今天到期" : `${r.days} 天後到期`),
  unassigned: () => "未指派",
};

// 使用者刪掉的提醒。分頁之後畫面會不斷重繪，光是把 DOM 節點移除撐不過翻頁，
// 得記在資料層才不會翻回來又冒出來。按「重新掃描」照樣全部復原。
const dismissedAlerts = new Set();

// 目前該顯示的提醒（後端掃描結果扣掉使用者刪掉的），首頁與側欄徽章也用這一份
function activeAlerts() {
  if (!lastReminders) return null;
  return [
    ...lastReminders.reminders.map(x => ({
      key: `r:${x.message}`, kind: x.kind, cls: `k-${x.kind}`, chip: ALERT_LABEL[x.kind](x), msg: x.message,
    })),
    ...lastReminders.followups.map(f => ({
      key: `f:${f.message}`, kind: "follow", cls: "k-follow", chip: "追問", msg: f.message,
    })),
  ].filter(a => !dismissedAlerts.has(a.key));
}

function updateAlertCount(n) {
  const badge = $("alertCount");
  badge.textContent = n;
  badge.classList.toggle("has", n > 0);
}

function renderReminders() {
  const alerts = activeAlerts();
  if (!alerts) return;
  const p = paginate("reminders", alerts);
  $("reminderBody").innerHTML = p.items.length
    ? p.items.map(a => `
        <div class="alert-item ${a.cls}" data-key="${esc(a.key)}">
          <span class="alert-chip">${esc(a.chip)}</span>
          <div class="alert-msg">${esc(a.msg)}</div>
          <button class="ghost copy-alert" data-copy="${esc(a.msg)}">複製</button>
          <button class="del-btn del-alert" title="刪除此提醒（按「重新掃描」可全部復原）" aria-label="刪除">${icon("x", "i-sm")}</button>
        </div>`).join("")
    : `<div class="empty-alert">${icon("check")}<p>尚無提醒</p></div>`;
  renderPager("reminders", p);
  updateAlertCount(alerts.length);
}

async function refreshReminders() {
  try {
    lastReminders = await jsonOrThrow(await fetch("/api/reminders"));
    dismissedAlerts.clear();  // 重新掃描＝把刪掉的那些全部找回來
    remindersLoaded = true;
    renderReminders();
    renderHome();
    maybeNotifyReminders(false);
  } catch (e) {
    remindersLoaded = true;
    $("reminderBody").innerHTML = loadFail("reminders");
    $("remindersPager").innerHTML = "";
    updateAlertCount(0);
    renderHome();
  }
}

// ---- 每日提醒通知（Notification API）----
let lastReminders = null;
const NOTIFY_KEY = "dailyNotify", NOTIFY_DATE_KEY = "dailyNotifyDate";
const notifyEnabled = () => localStorage.getItem(NOTIFY_KEY) === "1";
function updateNotifyBtn() {
  $("btnNotifyToggle").textContent = notifyEnabled() ? "已開啟" : "開啟";
}
function maybeNotifyReminders(force) {
  if (!notifyEnabled() || !("Notification" in window) || Notification.permission !== "granted") return;
  if (!lastReminders) return;
  const urgent = lastReminders.reminders.filter(x => x.kind === "overdue" || x.kind === "due_soon");
  if (!urgent.length) return;
  const today = new Date().toLocaleDateString("sv");
  if (!force && localStorage.getItem(NOTIFY_DATE_KEY) === today) return;  // 一天最多一次
  localStorage.setItem(NOTIFY_DATE_KEY, today);
  const overdue = urgent.filter(x => x.kind === "overdue").length;
  const soon = urgent.length - overdue;
  const parts = [overdue ? `${overdue} 項逾期` : "", soon ? `${soon} 項即將到期` : ""].filter(Boolean);
  try {
    new Notification("會議 Agent 待辦提醒", { body: parts.join("、") + "，點開看看吧。", icon: "/static/icon.svg" });
  } catch (e) { /* 部分瀏覽器需由 service worker 發送，失敗就略過 */ }
}
$("btnNotifyToggle").addEventListener("click", async () => {
  if (notifyEnabled()) { localStorage.setItem(NOTIFY_KEY, "0"); updateNotifyBtn(); return; }
  if (!("Notification" in window)) { showError("此瀏覽器不支援通知功能"); return; }
  let perm = Notification.permission;
  if (perm !== "granted") perm = await Notification.requestPermission();
  if (perm !== "granted") { showError("尚未允許通知權限，請到瀏覽器網站設定開啟"); return; }
  localStorage.setItem(NOTIFY_KEY, "1");
  updateNotifyBtn();
  maybeNotifyReminders(true);  // 開啟當下先示範一次
});
updateNotifyBtn();

$("reminderBody").addEventListener("click", async e => {
  // 刪除單則：只記在前端，不動後端；按「重新掃描」即可全部復原
  const del = e.target.closest(".del-alert");
  if (del) {
    dismissedAlerts.add(del.closest(".alert-item").dataset.key);
    renderReminders();
    renderHome();
    return;
  }
  const btn = e.target.closest(".copy-alert");
  if (!btn) return;
  await navigator.clipboard.writeText(btn.dataset.copy);
  const original = btn.textContent;
  btn.textContent = "已複製";
  setTimeout(() => (btn.textContent = original), 1500);
});
$("btnRefreshReminders").addEventListener("click", refreshReminders);
refreshReminders();

registerRefresher("reminders", refreshReminders);
registerPager("reminders", renderReminders);  // 讓 core 的翻頁按鈕知道要重繪誰

export { ALERT_LABEL, NOTIFY_KEY, activeAlerts, dismissedAlerts, lastReminders, maybeNotifyReminders, notifyEnabled, refreshReminders, remindersLoaded, renderReminders, updateAlertCount, updateNotifyBtn };
