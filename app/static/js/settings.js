import { api } from "./api.js";
import { $, esc, icon, showError } from "./core.js";
import { refreshMeetings } from "./meetings.js";
import { refreshReminders } from "./reminders.js";
import { refreshTasks } from "./tasks.js";

/* ==================================================================
   10. 介面與資料工具：主題、設定選單、自訂詞彙、備份還原、PWA、觸覺回饋
   ================================================================== */
// ---- 深淺色主題切換（記住偏好） ----
(function () {
  if (localStorage.getItem("theme") === "light") document.body.classList.add("light");
  $("themeToggle").addEventListener("click", () => {
    const light = document.body.classList.toggle("light");
    localStorage.setItem("theme", light ? "light" : "dark");
  });
})();

// ---- 設定選單（齒輪）----
(function () {
  const menu = $("settingsMenu");
  const btn = $("btnSettings");
  btn.addEventListener("click", async e => {
    e.stopPropagation();
    const open = menu.classList.toggle("open");
    btn.setAttribute("aria-expanded", open);
    if (open) {  // 打開時順便更新今日用量
      try {
        const u = await api.usage();
        const t = u.today || {};
        $("usageCalls").textContent = t.gemini_call || 0;
        $("usageAnalysis").textContent = t.analysis || 0;
        $("usageAsk").textContent = t.ask || 0;
        $("usageLive").textContent = t.live_chunk || 0;
      } catch (err) {
        $("usageCalls").textContent = $("usageAnalysis").textContent =
          $("usageAsk").textContent = $("usageLive").textContent = "—";
      }
    }
  });
  document.addEventListener("click", e => {
    if (!e.target.closest(".settings-wrap")) {
      menu.classList.remove("open");
      btn.setAttribute("aria-expanded", "false");
    }
  });
})();

// ---- 自訂詞彙管理 ----
let glosTerms = [];

function renderGlossary() {
  $("glosList").innerHTML = glosTerms.length
    ? glosTerms.map((t, i) => `<div class="glos-item">
        <b>${esc(t.term)}</b>
        ${t.note ? `<span class="glos-note">${esc(t.note)}</span>` : ""}
        <button class="del-btn" data-i="${i}" title="刪除此詞彙" aria-label="刪除">${icon("x", "i-sm")}</button>
      </div>`).join("")
    : `<p class="empty-note">尚無詞彙</p>`;
}

async function saveGlossary() {
  try {
    const r = await api.saveGlossary({ terms: glosTerms });
    glosTerms = r.terms;
    renderGlossary();
  } catch (err) { showError("儲存詞彙失敗：" + err.message); }
}

// ---- 資料還原（從備份 JSON 覆蓋現有資料）----
$("btnRestore").addEventListener("click", () => $("restoreFile").click());
$("restoreFile").addEventListener("change", async () => {
  const file = $("restoreFile").files[0];
  if (!file) return;
  if (!confirm("還原會用備份內容『覆蓋』目前所有會議與任務，現有資料將被取代。確定？")) {
    $("restoreFile").value = "";
    return;
  }
  try {
    const data = JSON.parse(await file.text());
    const r = await api.restore(data);
    $("settingsMenu").classList.remove("open");
    alert(`已還原 ${r.restored.meetings} 場會議、${r.restored.tasks} 筆任務。`);
    refreshTasks(); refreshMeetings(); refreshReminders();
  } catch (err) { showError("還原失敗：" + err.message); }
  finally { $("restoreFile").value = ""; }
});

$("btnGlossary").addEventListener("click", async () => {
  $("settingsMenu").classList.remove("open");
  $("glossaryModal").classList.add("open");
  try {
    glosTerms = (await api.glossary()).terms;
    renderGlossary();
  } catch (err) { /* 讀取失敗仍可新增 */ }
  $("glosTerm").focus();
});
$("btnGlossaryClose").addEventListener("click", () => $("glossaryModal").classList.remove("open"));
$("glossaryModal").addEventListener("click", e => {
  if (e.target === $("glossaryModal")) $("glossaryModal").classList.remove("open");
});
$("btnGlosAdd").addEventListener("click", () => {
  const term = $("glosTerm").value.trim();
  if (!term) return;
  glosTerms.push({ term, note: $("glosNote").value.trim() });
  $("glosTerm").value = "";
  $("glosNote").value = "";
  saveGlossary();
  $("glosTerm").focus();
});
$("glosTerm").addEventListener("keydown", e => { if (e.key === "Enter") $("btnGlosAdd").click(); });
$("glosNote").addEventListener("keydown", e => { if (e.key === "Enter") $("btnGlosAdd").click(); });
$("glosList").addEventListener("click", e => {
  const btn = e.target.closest(".del-btn");
  if (!btn) return;
  glosTerms.splice(Number(btn.dataset.i), 1);
  saveGlossary();
});

// ---- 講者名冊管理 ----
// 名冊只影響「講者辨識」時的姓名寫法，與自訂詞彙是兩件事，所以分開一個視窗。
let rosterNames = [];

function renderRoster() {
  $("rosterList").innerHTML = rosterNames.length
    ? rosterNames.map((n, i) => `<div class="glos-item">
        <b>${esc(n)}</b>
        <button class="del-btn" data-i="${i}" title="從名冊移除" aria-label="刪除">${icon("x", "i-sm")}</button>
      </div>`).join("")
    : `<p class="empty-note">尚無講者</p>`;
}

async function saveRoster() {
  try {
    const r = await api.saveSpeakers({ names: rosterNames });
    rosterNames = r.names;
  } catch (err) {
    showError("儲存講者名冊失敗：" + err.message);
    rosterNames = (await api.speakers()).names;  // 退回伺服器版本
  }
  renderRoster();
}

// 記一個剛用到的姓名（手動改講者名時呼叫）。名冊記不記得起來都不影響改名本身，
// 所以失敗只當沒發生，不打擾使用者
async function rememberSpeaker(name) {
  try {
    await api.rememberSpeakers({ names: [name] });
  } catch (err) { /* 名冊是加分項，靜靜略過 */ }
}

$("btnRoster").addEventListener("click", async () => {
  $("settingsMenu").classList.remove("open");
  $("rosterModal").classList.add("open");
  try {
    rosterNames = (await api.speakers()).names;
    renderRoster();
  } catch (err) { /* 讀取失敗仍可新增 */ }
  $("rosterName").focus();
});
$("btnRosterClose").addEventListener("click", () => $("rosterModal").classList.remove("open"));
$("rosterModal").addEventListener("click", e => {
  if (e.target === $("rosterModal")) $("rosterModal").classList.remove("open");
});
$("btnRosterAdd").addEventListener("click", () => {
  const name = $("rosterName").value.trim();
  if (!name) return;
  rosterNames.unshift(name);  // 新加的排最前面，與「最近用到的在前」一致
  $("rosterName").value = "";
  saveRoster();
  $("rosterName").focus();
});
$("rosterName").addEventListener("keydown", e => { if (e.key === "Enter") $("btnRosterAdd").click(); });
$("rosterList").addEventListener("click", e => {
  const btn = e.target.closest(".del-btn");
  if (!btn) return;
  rosterNames.splice(Number(btn.dataset.i), 1);
  saveRoster();
});

// ---- PWA：註冊 service worker（讓手機可「加入主畫面」以近原生方式使用） ----
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => { /* 不支援就當一般網頁 */ });
}

// ---- 觸覺回饋：按鈕/分頁按下時輕震（支援的裝置多為手機）；桌機靠 :active 視覺回饋 ----
(function () {
  const buzz = ms => { if (navigator.vibrate) { try { navigator.vibrate(ms); } catch (e) {} } };
  document.addEventListener("pointerdown", e => {
    if (e.target.closest("button, .tab")) buzz(8);
  }, { passive: true });
})();

export { glosTerms, rememberSpeaker, renderGlossary, renderRoster, rosterNames, saveGlossary, saveRoster };
