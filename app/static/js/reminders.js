import { api } from "./api.js";
import { $, esc, icon, loadFail, paginate, registerPager, registerRefresher, renderPager, showError } from "./core.js";
import { renderHome } from "./home.js";
import { allMeetings, meetingLabel } from "./meetings.js";

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

const MANUAL_KEY = "__manual__";  // 手動任務（無 meeting_id）的提醒歸這一組

// 群組展開狀態：key -> 是否展開。沒記錄過的用「是否緊急」當預設（含逾期/即將
// 到期的群組預設展開，其餘收合）；使用者手動開合後以他的選擇為準。整份重繪也
// 撐得過，因為狀態記在這裡而不是 DOM。
const reminderGroupState = new Map();

// 目前該顯示的提醒（後端掃描結果扣掉使用者刪掉的），首頁與側欄徽章也用這一份
function activeAlerts() {
  if (!lastReminders) return null;
  return [
    ...lastReminders.reminders.map(x => ({
      key: `r:${x.message}`, kind: x.kind, cls: `k-${x.kind}`, chip: ALERT_LABEL[x.kind](x), msg: x.message,
      meetingId: x.task?.meeting_id || null,
    })),
    ...lastReminders.followups.map(f => ({
      key: `f:${f.message}`, kind: "follow", cls: "k-follow", chip: "追問", msg: f.message,
      meetingId: f.meeting_id || null, meetingTitle: f.meeting_title,
    })),
  ].filter(a => !dismissedAlerts.has(a.key));
}

// 依會議把提醒分組，會議照 allMeetings 新到舊，手動任務墊底（同 tasks.js 的規則）
function groupAlerts(alerts) {
  const byKey = new Map();
  for (const a of alerts) {
    const key = a.meetingId || MANUAL_KEY;
    (byKey.get(key) || byKey.set(key, []).get(key)).push(a);
  }
  const order = [];
  for (const m of allMeetings) if (byKey.has(m.id)) order.push(m.id);
  for (const key of byKey.keys())
    if (key !== MANUAL_KEY && !order.includes(key)) order.push(key);
  if (byKey.has(MANUAL_KEY)) order.push(MANUAL_KEY);
  return order.map(key => ({ key, alerts: byKey.get(key) }));
}

const _URGENCY = { overdue: 0, due_soon: 1, follow: 2, unassigned: 3 };

function alertItemHtml(a) {
  return `<div class="alert-item ${a.cls}" data-key="${esc(a.key)}">
      <span class="alert-chip">${esc(a.chip)}</span>
      <div class="alert-msg">${esc(a.msg)}</div>
      <button class="ghost copy-alert" data-copy="${esc(a.msg)}">複製</button>
      <button class="del-btn del-alert" title="刪除此提醒（按「重新掃描」可全部復原）" aria-label="刪除">${icon("x", "i-sm")}</button>
    </div>`;
}

function alertGroupHtml(g) {
  // 群組取最緊急的一項當左緣顏色與（預設）展開依據——逾期的群組一眼就該看到
  const topKind = g.alerts.reduce((k, a) => _URGENCY[a.kind] < _URGENCY[k] ? a.kind : k, "unassigned");
  const urgent = topKind === "overdue" || topKind === "due_soon";
  const open = reminderGroupState.has(g.key) ? reminderGroupState.get(g.key) : urgent;

  const titleFromFollow = g.alerts.find(a => a.meetingTitle)?.meetingTitle;
  const label = g.key === MANUAL_KEY ? null : meetingLabel(g.key);
  const title = g.key === MANUAL_KEY
    ? "手動新增"
    : esc(label ? label.title : (titleFromFollow || "（找不到的會議）"));

  return `<details class="mtg-group k-${topKind}" data-key="${esc(g.key)}" ${open ? "open" : ""}>
      <summary class="mtg-summary">
        ${icon("chevron-right", "i-sm mtg-chevron")}
        <span class="mtg-title">${title}</span>
        <span class="mtg-count">${g.alerts.length}</span>
      </summary>
      <div class="alert-list">${g.alerts.map(alertItemHtml).join("")}</div>
    </details>`;
}

function updateAlertCount(n) {
  const badge = $("alertCount");
  badge.textContent = n;
  badge.classList.toggle("has", n > 0);
}

function renderReminders() {
  const alerts = activeAlerts();
  if (!alerts) return;
  const groups = groupAlerts(alerts);
  const p = paginate("reminders", groups);  // 分頁的是會議群組，不是單則提醒
  $("reminderBody").innerHTML = p.items.length
    ? p.items.map(alertGroupHtml).join("")
    : `<div class="empty-alert">${icon("check")}<p>尚無提醒</p></div>`;
  renderPager("reminders", p);
  updateAlertCount(alerts.length);  // 徽章數＝提醒總則數，不是群組數
}

async function refreshReminders() {
  try {
    lastReminders = await api.reminders();
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

// 記住群組展開／收合，重繪後才能還原（原生 <details> 撐不過 innerHTML 重設）
$("reminderBody").addEventListener("toggle", e => {
  const d = e.target.closest(".mtg-group");
  if (d) reminderGroupState.set(d.dataset.key, d.open);
}, true);  // toggle 不冒泡，用捕捉階段才收得到

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
