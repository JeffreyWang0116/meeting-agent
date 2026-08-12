import { $, esc, icon, jsonOrThrow, skelBlocks } from "./core.js";
import { allMeetings, meetingsLoaded, openMeetingDetail } from "./meetings.js";
import { activeAlerts, remindersLoaded } from "./reminders.js";
import { showView } from "./router.js";
import { allTasks, tasksLoaded } from "./tasks.js";

let todayAnalysis = 0;
let usageLoaded = false;

function renderHome() {
  const open = allTasks.filter(t => t.status !== "done");
  const alerts = activeAlerts();
  const urgent = alerts
    ? alerts.filter(a => a.kind === "overdue" || a.kind === "due_soon").length
    : null;

  // 還沒載完就放骨架，別先寫 0 再跳成真實數字——那會看起來像資料掉了又回來
  const num = (loaded, v) => (loaded ? String(v) : `<span class="skel skel-num"></span>`);
  $("statMeetings").innerHTML = num(meetingsLoaded, allMeetings.length);
  $("statOpen").innerHTML = num(tasksLoaded, open.length);
  $("statOverdue").innerHTML = num(remindersLoaded, urgent);
  $("statOverdue").classList.toggle("hot", urgent > 0);
  $("statUsage").innerHTML = num(usageLoaded, todayAnalysis);

  // 側欄徽章：不用切過去也知道那邊有幾件事在等
  $("navAlert").textContent = urgent || "";
  $("navAlert").hidden = !urgent;
  $("navTask").textContent = open.length || "";
  $("navTask").hidden = !open.length;

  $("homeMeetings").innerHTML = !meetingsLoaded
    ? skelBlocks(3)
    : allMeetings.length
    ? `<div class="home-list">${allMeetings.slice(0, 5).map(m => `
        <div class="home-row" data-meeting="${esc(m.id)}" title="開啟這場會議">
          <span class="home-row-main">
            <b>${esc(m.meeting.title)}</b>
            <span class="meta">${esc(m.meeting.date)}${m.kind ? ` · ${esc(m.kind)}` : ""}</span>
          </span>${icon("chevron-right")}
        </div>`).join("")}</div>`
    : `<p class="empty-note">尚無會議紀錄，從「新會議」開始第一場。</p>`;

  $("homeAlerts").innerHTML = !remindersLoaded
    ? skelBlocks(3)
    : (alerts || []).length
    ? alerts.slice(0, 4).map(a => `
        <div class="alert-item ${a.cls}">
          <span class="alert-chip">${esc(a.chip)}</span>
          <div class="alert-msg">${esc(a.msg)}</div>
        </div>`).join("")
    : `<p class="empty-note">目前沒有需要注意的事項。</p>`;
}

$("homeMeetings").addEventListener("click", e => {
  const row = e.target.closest("[data-meeting]");
  if (!row) return;
  showView("meeting");
  openMeetingDetail(row.dataset.meeting);
});

// 今日分析次數：設定選單也會用同一支 API，這裡先抓一次給首頁的數字磚
async function refreshUsage() {
  try {
    todayAnalysis = (await jsonOrThrow(await fetch("/api/usage"))).today?.analysis ?? 0;
  } catch (e) { todayAnalysis = "—"; }
  usageLoaded = true;
  renderHome();
}
refreshUsage();

export { refreshUsage, renderHome, todayAnalysis, usageLoaded };
