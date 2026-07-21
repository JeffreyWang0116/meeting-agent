/*
  主動式會議 Agent — 前端行為（app.js）
  畫面結構在 index.html、樣式在 style.css，三個檔案的功能分區順序一致。

  目錄：
    1. 共用基礎（API 認證、圖示、小工具）
    2. 全域初始化（日期、錄音種類、功能勾選、分頁）
    3. 逐字稿（連續文件式渲染、時間/引用句跳轉）
    4. 分析結果（摘要／會議重點／決議／代辦／行事曆／確認信）
    5. 任務庫
    6. 歷史會議
    7. 主動提醒與每日通知
    8. 跨會議問答（RAG＋搜尋）
    9. 輸入路徑（純文字／檔案上傳／即時聆聽）
   10. 介面與資料工具（面板收縮、主題、設定、詞彙、備份、PWA）
*/
"use strict";
const $ = id => document.getElementById(id);
const PRIORITY_ZH = { high: "高", medium: "中", low: "低" };
let chunkSeconds = 45;

/* ==================================================================
   1. 共用基礎：API 認證、圖示、esc／錯誤橫幅等小工具
   ================================================================== */
// ---- API 認證：伺服器設了 API_TOKEN 時，所有 /api/* 請求要帶 Authorization ----
const API_TOKEN_KEY = "apiToken";
const nativeFetch = window.fetch.bind(window);
window.fetch = async (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  if (url.startsWith("/api/")) {
    const token = localStorage.getItem(API_TOKEN_KEY);
    if (token) init = { ...init, headers: { ...(init.headers || {}), Authorization: `Bearer ${token}` } };
  }
  const resp = await nativeFetch(input, init);
  if (resp.status === 401 && url.startsWith("/api/")) {
    const entered = window.prompt("此伺服器需要 API Token 才能使用，請輸入：");
    if (entered) {
      localStorage.setItem(API_TOKEN_KEY, entered.trim());
      location.reload();
    }
  }
  return resp;
};

// ---- 圖示（線條風，24x24 viewBox，currentColor） ----
const ICON_PATHS = {
  calendar: '<rect x="3" y="4.5" width="18" height="16" rx="1.5"/><path d="M16 2.5v4M8 2.5v4M3 9.5h18"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4.5 21a7.5 7.5 0 0 1 15 0"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.8.4-1 .9-1 1.7"/><path d="M12 17h.01"/>',
  alert: '<path d="M12 9v4.5"/><path d="m10.6 3.5-9 15.6a1 1 0 0 0 .9 1.5h18.9a1 1 0 0 0 .9-1.5l-9-15.6a1 1 0 0 0-1.7 0Z"/><circle cx="12" cy="17" r=".6" fill="currentColor" stroke="none"/>',
  check: '<circle cx="12" cy="12" r="9"/><path d="m8.5 12 2.5 2.5L16 9"/>',
};
function icon(name, cls) {
  return `<svg class="${cls || "i"}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${ICON_PATHS[name] || ""}</svg>`;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function showError(msg) {
  const b = $("errorBanner");
  b.classList.remove("notice");
  b.innerHTML = icon("alert") + `<span>${esc(msg)}</span>`;
  b.style.display = "flex";
  b.scrollIntoView({ behavior: "smooth", block: "nearest" });
}
// 一般提示（成功、附帶說明），用同一條橫幅但不是紅色警示
function showNotice(msg) {
  const b = $("errorBanner");
  b.classList.add("notice");
  b.innerHTML = icon("check") + `<span>${esc(msg)}</span>`;
  b.style.display = "flex";
}
function clearError() { $("errorBanner").style.display = "none"; }
async function jsonOrThrow(resp) {
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.detail || `伺服器錯誤（${resp.status}）`);
  return body;
}

/* ==================================================================
   2. 全域初始化：會議日期、錄音種類、功能勾選、分頁切換
   ================================================================== */
// ---- 初始化 ----
$("meetingDate").value = new Date().toLocaleDateString("sv");  // YYYY-MM-DD（本地時區）

// 錄音種類：記住上次的選擇
(function () {
  const saved = localStorage.getItem("meetingKind");
  if (saved && [...$("meetingKind").options].some(o => o.value === saved)) {
    $("meetingKind").value = saved;
  }
  $("meetingKind").addEventListener("change", () => {
    localStorage.setItem("meetingKind", $("meetingKind").value);
    updateFeatureRowVisibility();
  });
})();

// 會議摘要／決議事項／代辦事項：只有錄音種類是「會議」時才有意義，
// 才顯示勾選框讓使用者決定要不要用（其他種類後端預設全部不用）
function updateFeatureRowVisibility() {
  $("featureRow").style.display = $("meetingKind").value === "會議" ? "flex" : "none";
}
updateFeatureRowVisibility();

// 目前的功能勾選狀態；錄音種類不是「會議」時回傳 null，讓後端套用該種類的預設值（不使用這些功能）
function selectedFeatures() {
  if ($("meetingKind").value !== "會議") return null;
  const features = [];
  if ($("featSummary").checked) features.push("summary");
  if ($("featHighlights").checked) features.push("highlights");
  if ($("featDecisions").checked) features.push("decisions");
  if ($("featTodos").checked) features.push("todos");
  return features;
}

// AI 校正錯字：與錄音種類無關（任何逐字稿都可能有同音錯字），
// 所以不跟著 selectedFeatures 的「非會議就回 null」規則走。預設關閉，記住選擇。
(function () {
  if (localStorage.getItem("correctTypos") === "1") $("featCorrect").checked = true;
  $("featCorrect").addEventListener("change", () =>
    localStorage.setItem("correctTypos", $("featCorrect").checked ? "1" : "0"));
})();
function correctTypos() { return $("featCorrect").checked; }

// 即時翻譯目標：記住上次的選擇
(function () {
  const saved = localStorage.getItem("liveTranslate");
  if (saved !== null && [...$("liveTranslate").options].some(o => o.value === saved)) {
    $("liveTranslate").value = saved;
  }
  $("liveTranslate").addEventListener("change", () =>
    localStorage.setItem("liveTranslate", $("liveTranslate").value));
})();

document.querySelectorAll(".tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".pane").forEach(p => p.classList.remove("active"));
    tab.classList.add("active");
    $(tab.dataset.pane).classList.add("active");
  });
});

async function loadHealth() {
  try {
    const h = await jsonOrThrow(await fetch("/api/health"));
    chunkSeconds = h.live_chunk_seconds || 45;
  } catch (e) { /* health 失敗不擋操作 */ }
}
loadHealth();

/* ==================================================================
   3. 逐字稿：連續文件式渲染（時間欄＋講者＋內文）與時間/引用句跳轉
   ================================================================== */
// ---- 連續文件式逐字稿渲染 ----
// 偵測行首「[1:02]」時間標記與「講者：」「Kevin:」等講者前綴。
// 帶時間標記的行自成一句（時間軸不能被合併吃掉）；沒有時間標記時維持
// 「同講者連續行合併」——轉錄結果常常一句一行，不合併會太碎。
const SPEAKER_RE = /^([^：:\n]{1,12})[：:]\s*/;
// 各段放寬成 1~2 位數：模型會吐出 [00]（漏掉「0:」）和 [0:1]（秒數一位數），
// 嚴格比對的話那些行會整個看不到時間（後端 segments.py 是同一套規則）
const TIME_RE = /^\[(\d{1,2}(?::\d{1,2}){0,2})\]\s*/;

// "1:02" / "1:02:03" → 秒數
function timeLabelToSeconds(label) {
  return String(label).split(":").reduce((acc, p) => acc * 60 + Number(p), 0);
}

function parseChatMessages(text) {
  const msgs = [];
  let lastSpeaker = null;
  for (const rawLine of String(text || "").split("\n")) {
    let line = rawLine.trim();
    if (!line) continue;
    const tm = line.match(TIME_RE);
    const time = tm ? tm[1] : null;
    if (tm) line = line.slice(tm[0].length).trim();
    if (!line) continue;
    const m = line.match(SPEAKER_RE);
    // 轉錄模型只在「換人講」時標註講者，同一人連續發言的後續行不再重複標籤
    // （逐字稿的標準慣例）。沒有標籤就沿用上一行的講者，否則那些續行會失去
    // 顏色分組、還會讓下一行重複顯示已經出現過的名字
    const speaker = m ? m[1].trim() : lastSpeaker;
    if (speaker) lastSpeaker = speaker;
    const content = m ? line.slice(m[0].length) : line;
    const last = msgs[msgs.length - 1];
    if (!time && last && last.speaker === speaker) {
      last.text += (last.text ? " " : "") + content;
      last.lines += 1;
    } else {
      msgs.push({ speaker, text: content, time, lines: 1 });
    }
  }
  return msgs;
}

// speakerMap：跨段落維持同講者同色（即時聆聽逐段附加時傳入同一個 map）
// translation：對應的譯文（可選）。行數對得上就逐句雙語對照，對不上就整段附在最後。
function chatHtml(text, speakerMap, translation) {
  const map = speakerMap || {};
  const colorOf = s => {
    if (!(s in map)) map[s] = Object.keys(map).length;
    return map[s] % 5;
  };
  const msgs = parseChatMessages(text);
  // 譯文沒有時間標記、合併方式可能與原文不同：先切成非空行，行數與原文
  // 總行數一致時，照原文每句合併的行數分組對齊
  let transTexts = null;
  if (translation) {
    const tLines = String(translation).split("\n")
      .map(l => l.trim().replace(TIME_RE, "")).filter(Boolean)
      .map(l => { const m = l.match(SPEAKER_RE); return m ? l.slice(m[0].length) : l; });
    const total = msgs.reduce((n, m) => n + m.lines, 0);
    if (msgs.length && tLines.length === total) {
      transTexts = []; let i = 0;
      for (const m of msgs) { transTexts.push(tLines.slice(i, i + m.lines).join(" ")); i += m.lines; }
    }
  }
  let html = "", prevSpeaker = null;
  msgs.forEach((m, i) => {
    const showName = m.speaker && m.speaker !== prevSpeaker;
    prevSpeaker = m.speaker;
    html += `<div class="chat-line ${m.speaker ? `sp-${colorOf(m.speaker)}` : ""}"${
        m.time ? ` data-t="${timeLabelToSeconds(m.time)}"` : ""}>` +
      `<span class="line-time">${m.time ? esc(m.time) : ""}</span>` +
      `<span class="line-body">${showName ? `<b class="line-speaker">${esc(m.speaker)}</b>` : ""}${esc(m.text)}` +
      (transTexts ? `<div class="line-trans">${esc(transTexts[i])}</div>` : "") +
      `</span></div>`;
  });
  if (translation && !transTexts) {  // 行數對不上：譯文整段補在後面
    html += `<div class="chat-line"><span class="line-time"></span><span class="line-body"><div class="line-trans">${esc(translation)}</div></span></div>`;
  }
  return html;
}

function renderChat(container, text) {
  container.innerHTML = chatHtml(text, {});
}

// ---- 逐字稿跳轉：依時間節點（優先）或引用句找到對應行，標亮並捲到可視範圍 ----
function findLineByQuote(lines, rawQuote) {
  // 引用句可能被截斷、改寫或跨行，逐步縮短前綴比對
  let quote = (rawQuote || "").replace(/\s+/g, "");
  let target = null;
  while (!target && quote.length > 4) {
    target = lines.find(l => l.textContent.replace(/\s+/g, "").includes(quote));
    if (!target) quote = quote.slice(0, Math.floor(quote.length * 0.7));
  }
  return target;
}

function jumpToTranscript(container, timeLabel, quote) {
  const lines = [...container.querySelectorAll(".chat-line")];
  lines.forEach(l => l.classList.remove("hl"));
  let target = null;
  if (timeLabel) {
    const t = timeLabelToSeconds(timeLabel);
    // 逐字稿依時間排序：取「時間 ≤ 目標」的最後一行（最接近又不超過）
    for (const l of lines) {
      if (l.dataset.t !== undefined && Number(l.dataset.t) <= t + 1) target = l;
    }
  }
  if (!target && quote) target = findLineByQuote(lines, quote);
  if (!target) return false;
  target.classList.add("hl");
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  return true;
}

/* ==================================================================
   4. 分析結果：摘要、會議重點、決議、代辦、行事曆、確認信
   ================================================================== */
// ---- AI 校正的錯字清單：讓使用者看得到到底改了哪些字，不是黑箱 ----
function renderCorrections(corrections) {
  const sec = $("rCorrSec");
  sec.style.display = corrections.length ? "block" : "none";
  sec.open = false;
  $("corrCount").textContent = corrections.length || "";
  $("rCorrections").innerHTML = corrections.map(c => `
    <div class="corr-item">
      <span class="corr-from">${esc(c.wrong)}</span>
      <span class="corr-arrow">→</span>
      <span class="corr-to">${esc(c.right)}</span>
      ${c.count > 1 ? `<span class="corr-count">×${c.count}</span>` : ""}
      ${c.reason ? `<span class="corr-why">${esc(c.reason)}</span>` : ""}
    </div>`).join("");
}

// ---- 結果渲染 ----
let currentTranscript = "";
let analysisStartTime = null;

function renderResult(result, transcript) {
  // 後端校正過的話，result.transcript 才是最終版本（傳進來的可能是校正前的）
  currentTranscript = (result.transcript || transcript || "").trim();
  $("rTransSec").style.display = currentTranscript ? "block" : "none";
  $("rTransSec").open = false;
  renderChat($("rTranscript"), currentTranscript);
  renderCorrections(result.corrections || []);
  const a = result.analysis, m = a.meeting;
  // 成效指標：這場會議 AI 幫你做了多少事、花了多久
  const elapsed = analysisStartTime ? ((Date.now() - analysisStartTime) / 1000).toFixed(1) : null;
  analysisStartTime = null;
  const statsChip =
    `<span class="meta-chip stats-chip">${currentTranscript ? `${currentTranscript.length} 字 → ` : ""}` +
    `${a.todos.length} 任務・${a.decisions.length} 決議${elapsed ? `・${elapsed}s` : ""}</span>`;
  $("rTitle").textContent = m.title;
  $("rMeta").innerHTML =
    `<span class="meta-chip">${icon("calendar", "i-sm")}${esc(m.date)}</span>` +
    `<span class="meta-chip">${esc($("meetingKind").value)}</span>` +
    m.attendees.map(p => `<span class="meta-chip">${icon("user", "i-sm")}${esc(p)}</span>`).join("") +
    (a.tags || []).map(t => `<span class="meta-chip"><span class="mtag">${esc(t)}</span></span>`).join("") +
    statsChip;
  // 摘要／決議／代辦：功能沒被使用（非會議種類、或使用者取消勾選）時整節隱藏，
  // 而不是顯示一個空空的區塊
  const kind = $("meetingKind").value;
  $("hSummary").style.display = m.summary ? "flex" : "none";
  $("rSummary").style.display = m.summary ? "block" : "none";
  $("rSummary").textContent = m.summary || "";
  // 摘要翻譯：中文摘要→譯成英文，外文摘要→譯成中文
  $("rSummaryTrans").style.display = "none";
  $("rSummaryTrans").textContent = "";
  $("transSummaryLabel").textContent = /[一-鿿]/.test(m.summary || "") ? "譯成英文" : "譯成中文";

  const highlights = a.highlights || [];
  const showHighlights = kind === "會議" || highlights.length > 0;
  $("hHighlights").style.display = showHighlights ? "flex" : "none";
  $("rHighlights").style.display = showHighlights ? "flex" : "none";
  $("rHighlights").innerHTML = highlights.length
    ? highlights.map(h => `
      <li class="hl-item" data-time="${esc(h.time || "")}" data-quote="${esc(h.source_quote || "")}" title="點擊跳到逐字稿出處">
        <span class="hl-text">${esc(h.text)}</span>
        ${h.time ? `<span class="hl-time">${esc(h.time)}</span>` : ""}
      </li>`).join("")
    : `<p class="empty-note">未擷取到會議重點</p>`;

  const showDecisions = kind === "會議" || a.decisions.length > 0;
  $("hDecisions").style.display = showDecisions ? "flex" : "none";
  $("rDecisions").style.display = showDecisions ? "flex" : "none";
  $("rDecisions").innerHTML = a.decisions.length
    ? a.decisions.map(d => `<li>${esc(d.description)}${d.context ? ` <span class="ctx">（${esc(d.context)}）</span>` : ""}</li>`).join("")
    : `<p class="empty-note">本次會議無正式決議</p>`;

  const showTodos = kind === "會議" || a.todos.length > 0;
  $("hTodos").style.display = showTodos ? "flex" : "none";
  $("rTodos").style.display = showTodos ? "flex" : "none";
  $("rTodos").innerHTML = a.todos.length
    ? a.todos.map(t => `
      <div class="todo-card p-${t.priority}">
        <div class="todo-head"><span class="led"></span><span class="todo-task">${esc(t.task)}</span></div>
        <div class="badges">
          <span class="badge ${t.owner ? "" : "owner-none"}">負責人 <b>${esc(t.owner || "未指派")}</b></span>
          <span class="badge">期限 <b>${esc(t.due_date || "未定")}</b></span>
          <span class="badge pr-${t.priority}" ${t.priority_reason ? `title="${esc(t.priority_reason)}"` : ""}>優先級 <b>${PRIORITY_ZH[t.priority]}</b></span>
        </div>
        ${t.priority_reason ? `<p class="why">判斷依據：${esc(t.priority_reason)}</p>` : ""}
        ${t.source_quote ? `<p class="quote clickable" data-quote="${esc(t.source_quote)}" title="點擊跳到逐字稿出處">${esc(t.source_quote)}</p>` : ""}
      </div>`).join("")
    : `<p class="empty-note">未偵測到代辦事項</p>`;

  $("rPending").innerHTML = a.pending_items.length
    ? a.pending_items.map(p => `<div class="pending-item">${icon("help")}<span>${esc(p.topic)}${p.reason ? `<span class="reason">${esc(p.reason)}</span>` : ""}</span></div>`).join("")
    : `<p class="empty-note">無</p>`;

  $("icsLink").href = `/api/meetings/${encodeURIComponent(result.meeting_id)}/events.ics`;
  const events = result.notifications.calendar_events || [];
  $("rEvents").innerHTML = events.length
    ? events.map(e => `<div class="event-item">${icon("calendar")}<span><span class="when">${esc(e.start.date)}</span><b>${esc(e.summary)}</b><span class="desc">${esc(e.description).replace(/\n/g, " · ")}</span></span></div>`).join("")
    : `<p class="empty-note">沒有含期限的代辦，未產生行事曆事件</p>`;

  $("rDraft").textContent = result.notifications.email_draft || "";
  $("result").style.display = "flex";
  $("result").scrollIntoView({ behavior: "smooth" });
  refreshTasks();
  refreshMeetings();
  refreshReminders();
}

// 複製按鈕通用行為：複製後短暫顯示「已複製」
async function copyWithFeedback(btn, text) {
  await navigator.clipboard.writeText(text);
  const original = btn.innerHTML;
  btn.textContent = "已複製";
  setTimeout(() => (btn.innerHTML = original), 1500);
}
$("btnCopyDraft").addEventListener("click", () => copyWithFeedback($("btnCopyDraft"), $("rDraft").textContent));

// 摘要翻譯（再按一次收起）
$("btnTransSummary").addEventListener("click", async () => {
  const box = $("rSummaryTrans");
  if (box.style.display !== "none") { box.style.display = "none"; return; }
  if (box.textContent) { box.style.display = "block"; return; }  // 已翻過，直接展開
  const summary = $("rSummary").textContent.trim();
  if (!summary) return;
  const target = /[一-鿿]/.test(summary) ? "en" : "zh";
  const label = $("transSummaryLabel");
  const original = label.textContent;
  label.textContent = "翻譯中…";
  try {
    const r = await jsonOrThrow(await fetch("/api/translate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: summary, target }),
    }));
    box.textContent = r.translation;
    box.style.display = "block";
  } catch (err) { showError("翻譯失敗：" + err.message); }
  finally { label.textContent = original; }
});
$("btnCopyTranscript").addEventListener("click", e => {
  e.preventDefault();  // 按鈕在 <summary> 裡，避免同時觸發展開/收合
  e.stopPropagation();
  copyWithFeedback($("btnCopyTranscript"), currentTranscript);
});

// ---- 逐字稿對照：點任務卡的引用句 → 展開逐字稿、標亮出處行 ----
$("rTodos").addEventListener("click", e => {
  const q = e.target.closest(".quote.clickable");
  if (!q || !currentTranscript) return;
  const sec = $("rTransSec");
  sec.style.display = "block";
  sec.open = true;
  if (!jumpToTranscript($("rTranscript"), null, q.dataset.quote)) {
    sec.scrollIntoView({ behavior: "smooth", block: "nearest" });  // 找不到就只展開逐字稿
  }
});

// ---- 會議重點：點擊 → 展開逐字稿、跳到該時間節點（沒有時間就用原句比對） ----
$("rHighlights").addEventListener("click", e => {
  const item = e.target.closest(".hl-item");
  if (!item || !currentTranscript) return;
  const sec = $("rTransSec");
  sec.style.display = "block";
  sec.open = true;
  if (!jumpToTranscript($("rTranscript"), item.dataset.time, item.dataset.quote)) {
    sec.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
});

/* ==================================================================
   5. 任務庫：清單、搜尋篩選、列內編輯、手動新增
   ================================================================== */
// ---- 資料庫 ----
const STATUS_ZH = { todo: "待辦", doing: "進行中", done: "完成" };
let allTasks = [];

let editingTaskId = null;  // 目前列內編輯中的任務

function taskRowHtml(t) {
  if (t.id === editingTaskId) {
    return `<tr>
        <td><input class="cell-input" id="editTask" value="${esc(t.task)}"></td>
        <td><input class="cell-input" id="editOwner" value="${esc(t.owner || "")}" placeholder="未指派"></td>
        <td><input class="cell-input" id="editDue" type="date" value="${esc(t.due_date || "")}"></td>
        <td><span class="pr-dot ${t.priority}"></span>${PRIORITY_ZH[t.priority] || esc(t.priority)}</td>
        <td>${STATUS_ZH[t.status] || esc(t.status)}</td>
        <td class="mono">${esc(t.meeting_id)}</td>
        <td><div class="row-ops">
          <button class="edit-btn save save-edit" data-id="${esc(t.id)}" title="儲存" aria-label="儲存">✓</button>
          <button class="del-btn cancel-edit" title="取消" aria-label="取消">✕</button>
        </div></td>
      </tr>`;
  }
  return `<tr class="${t.status === "done" ? "row-done" : ""}">
        <td>${esc(t.task)}</td>
        <td>${t.owner ? esc(t.owner) : `<span class="unassigned">未指派</span>`}</td>
        <td class="mono">${esc(t.due_date || "—")}</td>
        <td><span class="pr-dot ${t.priority}"></span>${PRIORITY_ZH[t.priority] || esc(t.priority)}</td>
        <td><select class="status-sel st-${t.status}" data-id="${esc(t.id)}">
          ${Object.entries(STATUS_ZH).map(([v, zh]) =>
            `<option value="${v}" ${v === t.status ? "selected" : ""}>${zh}</option>`).join("")}
        </select></td>
        <td class="mono">${esc(t.meeting_id)}</td>
        <td><div class="row-ops">
          <button class="edit-btn start-edit" data-id="${esc(t.id)}" title="編輯名稱、負責人、期限" aria-label="編輯">
            <svg class="i-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>
          </button>
          <button class="del-btn" data-id="${esc(t.id)}" title="刪除此任務" aria-label="刪除">✕</button>
        </div></td>
      </tr>`;
}

function renderTasks() {
  const q = $("taskSearch").value.trim().toLowerCase();
  const st = $("taskFilter").value;
  const rows = allTasks.filter(t =>
    (!st || t.status === st) &&
    (!q || `${t.task} ${t.owner || ""} ${t.meeting_id}`.toLowerCase().includes(q))
  );
  $("taskRows").innerHTML = rows.length
    ? rows.map(taskRowHtml).join("")
    : `<tr><td colspan="7" class="empty-note">${allTasks.length ? "沒有符合條件的任務" : "尚無任務"}</td></tr>`;
}

async function refreshTasks() {
  try {
    allTasks = (await jsonOrThrow(await fetch("/api/tasks"))).tasks;
    renderTasks();
  } catch (e) { /* 靜默 */ }
}

$("taskRows").addEventListener("change", async e => {
  const sel = e.target.closest(".status-sel");
  if (!sel) return;
  try {
    const updated = await jsonOrThrow(await fetch(`/api/tasks/${sel.dataset.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: sel.value }),
    }));
    const i = allTasks.findIndex(t => t.id === updated.id);
    if (i >= 0) allTasks[i] = updated;
    renderTasks();
    refreshReminders();  // 完成任務可能解除逾期提醒
  } catch (err) { showError("更新任務狀態失敗：" + err.message); refreshTasks(); }
});

$("taskRows").addEventListener("click", async e => {
  const start = e.target.closest(".start-edit");
  if (start) {
    editingTaskId = start.dataset.id;
    renderTasks();
    $("editTask").focus();
    return;
  }
  if (e.target.closest(".cancel-edit")) {
    editingTaskId = null;
    renderTasks();
    return;
  }
  const save = e.target.closest(".save-edit");
  if (save) {
    const fields = {
      task: $("editTask").value.trim(),
      owner: $("editOwner").value.trim() || null,
      due_date: $("editDue").value || null,
    };
    if (!fields.task) { showError("任務名稱不可為空"); return; }
    try {
      const updated = await jsonOrThrow(await fetch(`/api/tasks/${save.dataset.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(fields),
      }));
      const i = allTasks.findIndex(t => t.id === updated.id);
      if (i >= 0) allTasks[i] = updated;
      editingTaskId = null;
      renderTasks();
      refreshReminders();  // 期限改動可能新增/解除逾期提醒
    } catch (err) { showError("更新任務失敗：" + err.message); }
    return;
  }
  const btn = e.target.closest(".del-btn");
  if (!btn) return;
  if (!confirm("確定要刪除這筆任務？")) return;
  try {
    await jsonOrThrow(await fetch(`/api/tasks/${btn.dataset.id}`, { method: "DELETE" }));
    allTasks = allTasks.filter(t => t.id !== btn.dataset.id);
    renderTasks();
    refreshReminders();
  } catch (err) { showError("刪除任務失敗：" + err.message); }
});

// 編輯列快捷鍵：Enter 儲存、Esc 取消
$("taskRows").addEventListener("keydown", e => {
  if (!e.target.closest(".cell-input")) return;
  if (e.key === "Enter") $("taskRows").querySelector(".save-edit")?.click();
  if (e.key === "Escape") { editingTaskId = null; renderTasks(); }
});

$("taskSearch").addEventListener("input", renderTasks);
$("taskFilter").addEventListener("change", renderTasks);
$("btnRefreshTasks").addEventListener("click", () => { refreshTasks(); refreshMeetings(); });

// 手動新增任務（會議之外臨時想到的待辦）
$("btnAddTask").addEventListener("click", () => {
  const row = $("taskAddRow");
  const show = row.style.display === "none";
  row.style.display = show ? "flex" : "none";
  if (show) $("newTaskName").focus();
});
$("btnAddTaskCancel").addEventListener("click", () => { $("taskAddRow").style.display = "none"; });
async function submitNewTask() {
  const name = $("newTaskName").value.trim();
  if (!name) { showError("任務名稱不可為空"); return; }
  try {
    await jsonOrThrow(await fetch("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task: name,
        owner: $("newTaskOwner").value.trim() || null,
        due_date: $("newTaskDue").value || null,
        priority: $("newTaskPriority").value,
      }),
    }));
    $("newTaskName").value = ""; $("newTaskOwner").value = ""; $("newTaskDue").value = "";
    $("newTaskPriority").value = "medium";
    $("taskAddRow").style.display = "none";
    refreshTasks(); refreshReminders();
  } catch (err) { showError("新增任務失敗：" + err.message); }
}
$("btnAddTaskSave").addEventListener("click", submitNewTask);
$("newTaskName").addEventListener("keydown", e => { if (e.key === "Enter") submitNewTask(); });
$("newTaskOwner").addEventListener("keydown", e => { if (e.key === "Enter") submitNewTask(); });

/* ==================================================================
   6. 歷史會議：查閱、編輯、重新分析、分享、講者改名、刪除
   ================================================================== */
// ---- 歷史會議（查閱 / 編輯 / 重新分析 / 分享 / 講者改名 / 刪除） ----
let allMeetings = [];
let expandedMeetingId = null;      // 展開詳情中的會議
let detailEditing = false;
const meetingDetailCache = {};     // id -> 完整紀錄（含逐字稿）

function detectSpeakers(text) {
  const found = new Set();
  for (const line of String(text || "").split("\n")) {
    const m = line.trim().replace(TIME_RE, "").match(SPEAKER_RE);
    if (m) found.add(m[1].trim());
  }
  return [...found].slice(0, 12);
}

function meetingDetailHtml(id) {
  const d = meetingDetailCache[id];
  if (!d) return `<div class="meeting-detail"><p class="empty-note">載入中…</p></div>`;
  if (detailEditing) {
    return `<div class="meeting-detail detail-edit">
        <label class="lbl">標題</label>
        <input type="text" id="dTitle" value="${esc(d.meeting.title)}">
        <label class="lbl">分類標籤（用「、」或逗號分隔，可自訂）</label>
        <input type="text" id="dTags" value="${esc((d.tags || []).join("、"))}">
        <label class="lbl">AI 摘要</label>
        <textarea id="dSummary" rows="4">${esc(d.meeting.summary || "")}</textarea>
        <label class="lbl">逐字稿全文</label>
        <textarea id="dTranscript" rows="12">${esc(d.transcript || "")}</textarea>
        <div class="detail-actions">
          <button class="primary save-detail" data-id="${esc(id)}">儲存</button>
          <button class="ghost cancel-detail">取消</button>
        </div>
      </div>`;
  }
  const decisions = (d.decisions || [])
    .map(x => `<li>${esc(x.description)}${x.context ? `（${esc(x.context)}）` : ""}</li>`).join("");
  const highlights = (d.highlights || [])
    .map(h => `<li class="hl-item detail-hl" data-time="${esc(h.time || "")}" data-quote="${esc(h.source_quote || "")}" title="點擊跳到逐字稿出處">
        <span class="hl-text">${esc(h.text)}</span>
        ${h.time ? `<span class="hl-time">${esc(h.time)}</span>` : ""}
      </li>`).join("");
  const speakers = detectSpeakers(d.transcript);
  return `<div class="meeting-detail">
      ${d.meeting.summary ? `<h4>AI 摘要 <button class="ghost trans-detail" data-id="${esc(id)}">翻譯</button></h4>
      <p class="detail-summary">${esc(d.meeting.summary)}</p>
      <div class="summary-trans" id="dSummaryTrans" style="display:none"></div>` : ""}
      ${highlights ? `<h4>會議重點</h4><ol class="highlight-list">${highlights}</ol>` : ""}
      ${decisions ? `<h4>決議事項</h4><ol class="detail-decisions">${decisions}</ol>` : ""}
      ${speakers.length ? `<h4>講者（點擊改名，整份逐字稿跟著更新）</h4>
        <div class="speaker-chips">${speakers.map(s =>
          `<span class="speaker-chip" data-id="${esc(id)}" data-speaker="${esc(s)}">✎ ${esc(s)}</span>`).join("")}</div>` : ""}
      <h4>完整逐字稿 <button class="ghost copy-detail" data-id="${esc(id)}">複製全文</button></h4>
      <div class="detail-transcript" id="dTranscriptView"></div>
      <div class="detail-actions">
        <button class="ghost edit-detail" data-id="${esc(id)}">
          <svg class="i-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>
          編輯
        </button>
        <button class="ghost reanalyze-detail" data-id="${esc(id)}">
          <svg class="i-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11a9 9 0 1 1-2.7-6.4"/><path d="M21 3v6h-6"/></svg>
          重新分析
        </button>
        <button class="ghost share-detail" data-id="${esc(id)}">
          <svg class="i-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8.6 13.5 6.8 4M15.4 6.5l-6.8 4"/></svg>
          分享
        </button>
        <a class="ghost btn-link" href="/api/meetings/${esc(id)}/events.ics" download>加入行事曆</a>
      </div>
    </div>`;
}

let activeTag = "";  // 歷史會議標籤篩選（"" = 全部）

function renderTagFilter() {
  const tags = new Set();
  allMeetings.forEach(m => {
    if (m.kind) tags.add(m.kind);
    (m.tags || []).forEach(t => tags.add(t));
  });
  const bar = $("tagFilter");
  if (!tags.size) { bar.style.display = "none"; return; }
  if (activeTag && !tags.has(activeTag)) activeTag = "";
  bar.style.display = "flex";
  bar.innerHTML = [`<span class="tag-chip ${activeTag ? "" : "active"}" data-tag="">全部</span>`]
    .concat([...tags].map(t =>
      `<span class="tag-chip ${t === activeTag ? "active" : ""}" data-tag="${esc(t)}">${esc(t)}</span>`))
    .join("");
}

function renderMeetings() {
  renderTagFilter();
  const shown = allMeetings.filter(m =>
    !activeTag || m.kind === activeTag || (m.tags || []).includes(activeTag));
  $("meetingRows").innerHTML = shown.length
    ? shown.map(m => `<div class="meeting-item ${m.id === expandedMeetingId ? "expanded" : ""}">
          <span class="meeting-info">
            <b>${esc(m.meeting.title)}</b>
            <span class="meeting-meta mono">${esc(m.meeting.date)}${m.kind ? ` · ${esc(m.kind)}` : ""} · ${esc(m.id)}</span>
            ${(m.tags || []).length ? `<span class="meeting-tags">${m.tags.map(t => `<span class="mtag">${esc(t)}</span>`).join("")}</span>` : ""}
          </span>
          <span class="meeting-ops">
            <button class="ghost view-meeting" data-id="${esc(m.id)}">${m.id === expandedMeetingId ? "收合" : "查閱"}</button>
            <a class="ghost btn-link" href="/api/meetings/${esc(m.id)}/report.md" download>下載</a>
            <button class="del-btn del-meeting" data-id="${esc(m.id)}" title="刪除此會議與其任務" aria-label="刪除">✕</button>
          </span>
        </div>` + (m.id === expandedMeetingId ? meetingDetailHtml(m.id) : "")).join("")
    : `<p class="empty-note">${allMeetings.length ? "沒有符合此標籤的會議" : "尚無會議紀錄"}</p>`;
  // 逐字稿要等 DOM 建立後渲染
  const view = document.getElementById("dTranscriptView");
  const d = meetingDetailCache[expandedMeetingId];
  if (view && d) {
    if (d.transcript) renderChat(view, d.transcript);
    else view.innerHTML = `<p class="empty-note">此會議沒有存逐字稿全文</p>`;
  }
}

async function refreshMeetings() {
  try {
    allMeetings = (await jsonOrThrow(await fetch("/api/meetings"))).meetings;
    renderMeetings();
    renderAskScope();
  } catch (e) { /* 靜默 */ }
}

async function openMeetingDetail(id) {
  expandedMeetingId = id;
  detailEditing = false;
  renderMeetings();  // 先顯示「載入中」
  if (!meetingDetailCache[id]) {
    try {
      meetingDetailCache[id] = await jsonOrThrow(await fetch(`/api/meetings/${id}`));
    } catch (err) { showError("讀取會議失敗：" + err.message); return; }
  }
  renderMeetings();
}

function meetingShareText(d) {
  const parts = [`${d.meeting.title}（${d.meeting.date}）`];
  if (d.meeting.summary) parts.push(`【AI 摘要】\n${d.meeting.summary}`);
  if ((d.highlights || []).length) {
    parts.push("【會議重點】\n" + d.highlights.map((h, i) =>
      `${i + 1}. ${h.text}${h.time ? `（${h.time}）` : ""}`).join("\n"));
  }
  if ((d.decisions || []).length) {
    parts.push("【決議事項】\n" + d.decisions.map((x, i) => `${i + 1}. ${x.description}`).join("\n"));
  }
  if (d.transcript) parts.push(`【完整逐字稿】\n${d.transcript}`);
  return parts.join("\n\n");
}

$("meetingRows").addEventListener("click", async e => {
  const view = e.target.closest(".view-meeting");
  if (view) {
    if (expandedMeetingId === view.dataset.id) { expandedMeetingId = null; renderMeetings(); }
    else openMeetingDetail(view.dataset.id);
    return;
  }
  const del = e.target.closest(".del-meeting");
  if (del) {
    if (!confirm("確定要刪除這場會議？它的任務也會一併刪除。")) return;
    try {
      await jsonOrThrow(await fetch(`/api/meetings/${del.dataset.id}`, { method: "DELETE" }));
      delete meetingDetailCache[del.dataset.id];
      if (expandedMeetingId === del.dataset.id) expandedMeetingId = null;
      refreshMeetings(); refreshTasks(); refreshReminders();
    } catch (err) { showError("刪除會議失敗：" + err.message); }
    return;
  }
  // 會議重點 → 跳到逐字稿的時間節點
  const hl = e.target.closest(".detail-hl");
  if (hl) {
    const view = document.getElementById("dTranscriptView");
    if (view) jumpToTranscript(view, hl.dataset.time, hl.dataset.quote);
    return;
  }

  if (e.target.closest(".edit-detail")) { detailEditing = true; renderMeetings(); return; }
  if (e.target.closest(".cancel-detail")) { detailEditing = false; renderMeetings(); return; }

  const save = e.target.closest(".save-detail");
  if (save) {
    try {
      const updated = await jsonOrThrow(await fetch(`/api/meetings/${save.dataset.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: $("dTitle").value.trim() || "未命名會議",
          summary: $("dSummary").value.trim(),
          transcript: $("dTranscript").value,
          tags: $("dTags").value.split(/[、,，\s]+/).map(t => t.trim()).filter(Boolean),
        }),
      }));
      meetingDetailCache[save.dataset.id] = updated;
      detailEditing = false;
      refreshMeetings();
    } catch (err) { showError("儲存會議失敗：" + err.message); }
    return;
  }

  const rean = e.target.closest(".reanalyze-detail");
  if (rean) {
    const correct = correctTypos();
    const note = correct ? "（含 AI 校正錯字，會改寫逐字稿）" : "";
    if (!confirm(`重新分析會用目前的逐字稿重跑 AI${note}，這場會議的任務會整批換新。繼續？`)) return;
    rean.disabled = true; rean.textContent = "分析中…";
    try {
      const r = await jsonOrThrow(await fetch(`/api/meetings/${rean.dataset.id}/reanalyze`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ correct_typos: correct }),
      }));
      const cached = meetingDetailCache[rean.dataset.id];
      if (cached) {
        cached.meeting = r.analysis.meeting;
        cached.decisions = r.analysis.decisions;
        cached.pending_items = r.analysis.pending_items;
        cached.highlights = r.analysis.highlights || [];
        cached.tags = r.analysis.tags || [];
        if (r.corrections && r.corrections.length) cached.transcript = r.transcript;
      }
      if (r.corrections && r.corrections.length) {
        showNotice(`AI 校正了 ${r.corrections.length} 處錯字：` +
          r.corrections.slice(0, 5).map(c => `${c.wrong}→${c.right}`).join("、"));
      }
      refreshMeetings(); refreshTasks(); refreshReminders();
    } catch (err) { showError("重新分析失敗：" + err.message); refreshMeetings(); }
    return;
  }

  const share = e.target.closest(".share-detail");
  if (share) {
    const d = meetingDetailCache[share.dataset.id];
    if (!d) return;
    const text = meetingShareText(d);
    if (navigator.share) {
      try { await navigator.share({ title: d.meeting.title, text }); } catch (err) { /* 使用者取消 */ }
    } else {
      await copyWithFeedback(share, text);  // 桌機沒有系統分享 → 複製全文
    }
    return;
  }

  const copyBtn = e.target.closest(".copy-detail");
  if (copyBtn) {
    const d = meetingDetailCache[copyBtn.dataset.id];
    if (d) await copyWithFeedback(copyBtn, d.transcript || "");
    return;
  }

  const trans = e.target.closest(".trans-detail");
  if (trans) {
    const d = meetingDetailCache[trans.dataset.id];
    const box = document.getElementById("dSummaryTrans");
    if (!d || !d.meeting.summary || !box) return;
    if (box.style.display !== "none") { box.style.display = "none"; return; }
    if (box.textContent) { box.style.display = "block"; return; }
    trans.textContent = "翻譯中…";
    try {
      const r = await jsonOrThrow(await fetch("/api/translate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: d.meeting.summary,
          target: /[一-鿿]/.test(d.meeting.summary) ? "en" : "zh",
        }),
      }));
      box.textContent = r.translation;
      box.style.display = "block";
    } catch (err) { showError("翻譯失敗：" + err.message); }
    finally { trans.textContent = "翻譯"; }
    return;
  }

  const chip = e.target.closest(".speaker-chip");
  if (chip) {
    const id = chip.dataset.id;
    const d = meetingDetailCache[id];
    if (!d || !d.transcript) return;
    const oldName = chip.dataset.speaker;
    const newName = (prompt(`把「${oldName}」改名為：`, oldName) || "").trim();
    if (!newName || newName === oldName) return;
    const renamed = d.transcript.split("\n").map(line => {
      const t = line.trimStart().replace(TIME_RE, "");  // 行首可能還有 [1:02] 時間標記
      if (t.startsWith(oldName + "：") || t.startsWith(oldName + ":")) {
        return line.replace(oldName, newName);  // 只換行首的講者標註
      }
      return line;
    }).join("\n");
    // 出席者名單裡的舊名一併換成新名（下游不再殘留「講者A」）
    const attendees = (d.meeting.attendees || []).map(a => (a === oldName ? newName : a));
    try {
      meetingDetailCache[id] = await jsonOrThrow(
        await fetch(`/api/meetings/${id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ transcript: renamed, attendees }),
        }));
      // 這場會議中「負責人＝舊名」的任務也跟著改名
      const owned = allTasks.filter(t => t.meeting_id === id && t.owner === oldName);
      for (const t of owned) {
        await fetch(`/api/tasks/${t.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ owner: newName }),
        });
      }
      renderMeetings();
      if (owned.length) { refreshTasks(); refreshReminders(); }
    } catch (err) { showError("講者改名失敗：" + err.message); }
  }
});

refreshTasks();
refreshMeetings();

/* ==================================================================
   7. 主動提醒：到期掃描與每日通知
   ================================================================== */
// ---- 主動提醒（Agent 自主掃描） ----
const ALERT_LABEL = {
  overdue: r => `逾期 ${r.days} 天`,
  due_soon: r => (r.days === 0 ? "今天到期" : `${r.days} 天後到期`),
  unassigned: () => "未指派",
};

// 依目前畫面上還剩幾則提醒更新標題的數字（刪除單項後也會即時反映）
function updateAlertCount() {
  const n = $("reminderBody").querySelectorAll(".alert-item").length;
  const badge = $("alertCount");
  badge.textContent = n;
  badge.classList.toggle("has", n > 0);
}

async function refreshReminders() {
  try {
    const r = await jsonOrThrow(await fetch("/api/reminders"));
    const alertItem = (cls, chip, msg) => `
        <div class="alert-item ${cls}">
          <span class="alert-chip">${chip}</span>
          <div class="alert-msg">${esc(msg)}</div>
          <button class="ghost copy-alert" data-copy="${esc(msg)}">複製</button>
          <button class="del-btn del-alert" title="刪除此提醒（按「重新掃描」可全部復原）" aria-label="刪除">✕</button>
        </div>`;
    const items = [
      ...r.reminders.map(x => alertItem(`k-${x.kind}`, ALERT_LABEL[x.kind](x), x.message)),
      ...r.followups.map(f => alertItem("k-follow", "追問", f.message)),
    ];
    $("reminderBody").innerHTML = items.length
      ? items.join("")
      : `<div class="empty-alert">${icon("check")}<p>尚無提醒</p></div>`;
    updateAlertCount();
    lastReminders = r;
    maybeNotifyReminders(false);
  } catch (e) { /* 靜默：提醒失敗不擋主流程 */ }
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
  // 刪除單則：只移除畫面，不動後端；按「重新掃描」即可全部復原
  const del = e.target.closest(".del-alert");
  if (del) {
    del.closest(".alert-item").remove();
    updateAlertCount();
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

// 標籤篩選
$("tagFilter").addEventListener("click", e => {
  const chip = e.target.closest(".tag-chip");
  if (!chip) return;
  activeTag = chip.dataset.tag;
  renderMeetings();
});

/* ==================================================================
   8. 跨會議問答：RAG 問答＋關鍵字即時搜尋
   ================================================================== */
// ---- 跨會議問答（RAG） ----
// 範圍複選：勾了哪些會議就只在那些會議裡檢索；都不勾 = 全部
const askScopeIds = new Set();

function renderAskScope() {
  const list = $("askScopeList");
  [...askScopeIds].forEach(id => {  // 會議被刪掉時同步移除
    if (!allMeetings.some(m => m.id === id)) askScopeIds.delete(id);
  });
  list.innerHTML = allMeetings.length
    ? allMeetings.map(m => `<label>
        <input type="checkbox" value="${esc(m.id)}" ${askScopeIds.has(m.id) ? "checked" : ""}>
        <span>${esc(m.meeting.title)}</span>
        <span class="meta">${esc(m.meeting.date)}</span>
      </label>`).join("")
    : `<p class="empty-note">尚無會議</p>`;
  $("askScopeSummary").textContent = askScopeIds.size
    ? `選取會議（已選 ${askScopeIds.size} 場）`
    : "選取會議";
}

$("askScopeList").addEventListener("change", e => {
  const cb = e.target.closest("input[type=checkbox]");
  if (!cb) return;
  if (cb.checked) askScopeIds.add(cb.value);
  else askScopeIds.delete(cb.value);
  renderAskScope();
});

async function sendAsk() {
  const q = $("askInput").value.trim();
  if (!q) return;
  $("askInput").value = "";
  hideSearchHits();
  const log = $("askLog");
  log.style.display = "flex";
  log.insertAdjacentHTML("beforeend",
    `<div class="ask-item">
       <button class="del-btn del-ask" title="刪除這則問答" aria-label="刪除">✕</button>
       <div class="ask-q"><span>Q</span><div>${esc(q)}</div></div>
       <div class="ask-a pending"><span>A</span><div>檢索會議紀錄中…</div></div>
     </div>`);
  const slot = log.lastElementChild.querySelector(".ask-a");
  log.scrollTop = log.scrollHeight;
  $("btnAsk").disabled = true;
  try {
    const r = await jsonOrThrow(await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: q,
        meeting_ids: askScopeIds.size ? [...askScopeIds] : null,
      }),
    }));
    slot.classList.remove("pending");
    slot.innerHTML = `<span>A</span><div>${esc(r.answer)}${
      r.sources && r.sources.length
        ? `<div class="ask-src">${r.sources.map(s => `<span class="src-chip">${esc(s.title)} · ${esc(s.date)}</span>`).join("")}</div>`
        : ""
    }</div>`;
  } catch (err) {
    slot.classList.remove("pending");
    slot.classList.add("err");
    slot.innerHTML = `<span>!</span><div>${esc(err.message)}</div>`;
  } finally {
    $("btnAsk").disabled = false;
    log.scrollTop = log.scrollHeight;
  }
}
$("btnAsk").addEventListener("click", sendAsk);
$("askInput").addEventListener("keydown", e => { if (e.key === "Enter") sendAsk(); });

// ---- 一框兩用：打字即時關鍵字搜尋（精確比對），按查詢才是問 AI ----
let searchTimer = null, searchSeq = 0;

function hideSearchHits() { $("askSearchHits").style.display = "none"; }

function snippetHtml(snippet, keyword) {
  const idx = snippet.toLowerCase().indexOf(keyword.toLowerCase());
  if (idx < 0) return esc(snippet);
  return esc(snippet.slice(0, idx)) +
    `<mark>${esc(snippet.slice(idx, idx + keyword.length))}</mark>` +
    esc(snippet.slice(idx + keyword.length));
}

$("askInput").addEventListener("input", () => {
  clearTimeout(searchTimer);
  const kw = $("askInput").value.trim();
  if (kw.length < 2) { hideSearchHits(); return; }
  searchTimer = setTimeout(async () => {
    const seq = ++searchSeq;
    try {
      const r = await jsonOrThrow(await fetch(`/api/search?q=${encodeURIComponent(kw)}`));
      if (seq !== searchSeq) return;  // 已有更新的搜尋，丟棄舊結果
      const box = $("askSearchHits");
      if (!r.hits.length) { hideSearchHits(); return; }
      box.innerHTML =
        `<span class="ask-hits-head">含「${esc(r.keyword)}」的會議（點擊開啟）</span>` +
        r.hits.map(h => `<div class="ask-hit" data-id="${esc(h.meeting_id)}">
            <b>${esc(h.title)}</b>
            <span class="snippet">${snippetHtml(h.snippet, r.keyword)}</span>
            <span class="meta">${esc(h.date)} · ${esc(h.field)}</span>
          </div>`).join("");
      box.style.display = "flex";
    } catch (err) { hideSearchHits(); }
  }, 300);
});

$("askSearchHits").addEventListener("click", e => {
  const hit = e.target.closest(".ask-hit");
  if (!hit) return;
  hideSearchHits();
  const panel = $("meetingPanel");
  panel.classList.remove("collapsed");  // 歷史面板若收合先展開
  openMeetingDetail(hit.dataset.id);
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
});
// 每則問答各自刪除；刪到全空就把整個對話框收起來
$("askLog").addEventListener("click", e => {
  const del = e.target.closest(".del-ask");
  if (!del) return;
  del.closest(".ask-item").remove();
  const log = $("askLog");
  if (!log.querySelector(".ask-item")) log.style.display = "none";
});

/* ==================================================================
   9. 輸入路徑：純文字貼上、檔案上傳（含拖曳）、即時聆聽
   ================================================================== */
// ---- 路徑 1：純文字 ----
$("btnAnalyzeText").addEventListener("click", async () => {
  clearError();
  const btn = $("btnAnalyzeText");
  const original = btn.innerHTML;
  btn.disabled = true; btn.textContent = "AI 分析中…";
  analysisStartTime = Date.now();
  try {
    const result = await jsonOrThrow(await fetch("/api/meetings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: $("textInput").value,
        meeting_date: $("meetingDate").value || null,
        kind: $("meetingKind").value,
        features: selectedFeatures(),
        correct_typos: correctTypos(),
      }),
    }));
    renderResult(result, $("textInput").value);
  } catch (e) { showError(e.message); }
  finally { btn.disabled = false; btn.innerHTML = original; }
});

// ---- 路徑 2：檔案上傳 ----
const JOB_STATUS_ZH = {
  queued: "排隊中…", extracting: "從影片抽取聲音軌…", transcribing: "轉錄中",
  analyzing: "AI 分析中…", done: "完成", error: "失敗",
};

$("btnUpload").addEventListener("click", async () => {
  clearError();
  const file = $("fileInput").files[0];
  if (!file) { showError("請先選擇檔案"); return; }

  const btn = $("btnUpload");
  btn.disabled = true;
  analysisStartTime = Date.now();
  $("fileProgress").style.display = "block";
  $("fileTranscript").style.display = "block";
  $("fileTranscript").textContent = "";

  try {
    const form = new FormData();
    form.append("file", file);
    if ($("meetingDate").value) form.append("meeting_date", $("meetingDate").value);
    form.append("kind", $("meetingKind").value);
    const features = selectedFeatures();
    if (features !== null) form.append("features", features.join(","));
    if (correctTypos()) form.append("correct_typos", "true");
    const { job_id } = await jsonOrThrow(await fetch("/api/media", { method: "POST", body: form }));

    while (true) {
      await new Promise(r => setTimeout(r, 1500));
      const job = await jsonOrThrow(await fetch(`/api/media/${job_id}`));
      const pct = Math.round((job.progress || 0) * 100);
      $("fileProgress").firstElementChild.style.width = pct + "%";
      $("fileStatus").textContent =
        JOB_STATUS_ZH[job.status] + (job.status === "transcribing" ? `（${pct}%）` : "");
      if (job.transcript) {
        renderChat($("fileTranscript"), job.transcript);
        $("fileTranscript").scrollTop = $("fileTranscript").scrollHeight;
      }
      if (job.status === "done") { renderResult(job.result, job.transcript); break; }
      if (job.status === "error") throw new Error(job.error || "轉錄失敗");
    }
  } catch (e) { showError(e.message); $("fileStatus").textContent = "失敗"; }
  finally { btn.disabled = false; }
});

// ---- 拖曳上傳 ----
(function () {
  const zone = $("dropZone");
  ["dragenter", "dragover"].forEach(ev =>
    zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach(ev =>
    zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.remove("dragover"); }));
  zone.addEventListener("drop", e => {
    const files = e.dataTransfer && e.dataTransfer.files;
    if (!files || !files.length) return;
    $("fileInput").files = files;
    $("fileStatus").textContent = `已選擇：${files[0].name}`;
  });
})();

// ---- 路徑 3：即時聆聽 ----
let liveStream = null, liveRecorder = null, liveSessionId = null;
let liveRecording = false, liveSegTimer = null, uploadsInFlight = 0, liveStartTime = null, liveTickTimer = null;
let liveSegIndex = 0, liveSentCount = 0, liveWakeLock = null, liveStarting = false;

// 手機螢幕熄滅會讓瀏覽器暫停錄音 → 聆聽期間用 Wake Lock 保持螢幕常亮
async function acquireWakeLock() {
  if (!("wakeLock" in navigator)) return;
  try { liveWakeLock = await navigator.wakeLock.request("screen"); } catch (e) { /* 被拒僅代表螢幕可能自動熄滅 */ }
}
function releaseWakeLock() {
  if (liveWakeLock) { try { liveWakeLock.release(); } catch (e) {} liveWakeLock = null; }
}
document.addEventListener("visibilitychange", () => {
  if (!liveRecording || document.visibilityState !== "visible") return;
  acquireWakeLock();  // 切回前景時螢幕鎖會被系統釋放，要重新取得
  if (liveRecorder && liveRecorder.state === "inactive") recordSegment();  // 錄音若被系統中斷則自動接續
});

function pickMime() {
  for (const m of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"]) {
    if (MediaRecorder.isTypeSupported(m)) return m;
  }
  return "";
}

function liveTick() {
  if (!liveRecording) return;
  const s = Math.floor((Date.now() - liveStartTime) / 1000);
  const pending = uploadsInFlight > 0 ? `，${uploadsInFlight} 段辨識中…` : "";
  const sent = liveSentCount > 0 ? `已送出 ${liveSentCount} 段${pending}` : `第一段約 ${Math.min(12, chunkSeconds)} 秒後送出`;
  $("liveStatus").innerHTML =
    `<span class="rec-dot"></span>聆聽中 ${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}（${sent}）`;
}

// 即時字幕流：逐段附加逐字稿行（後端回傳的文字已帶整場時間戳），末端保留打字游標。
// liveSpeakers 讓同一位講者在整場聆聽中維持同色。
let liveSpeakers = {};
function appendCaption(text, translation) {
  const caret = $("liveCaret");
  if (!caret) return;
  caret.insertAdjacentHTML("beforebegin", chatHtml(text, liveSpeakers, translation));
  $("liveTranscript").scrollTop = $("liveTranscript").scrollHeight;
}

async function uploadLiveChunk(blob, offsetSeconds) {
  uploadsInFlight++;
  liveSentCount++;
  try {
    const ext = blob.type.includes("mp4") ? ".mp4" : ".webm";
    const form = new FormData();
    form.append("file", blob, "chunk" + ext);
    // 本段在整場會議中的開始秒數：後端把段內相對時間戳平移成整場時間
    if (offsetSeconds != null) form.append("offset", offsetSeconds);
    const r = await jsonOrThrow(await fetch(`/api/live/${liveSessionId}/chunk`, { method: "POST", body: form }));
    if (r.text) appendCaption(r.text, r.translation);
  } catch (e) { showError("音訊段上傳失敗：" + e.message); }
  finally { uploadsInFlight--; }
}

// 每段用「新的 MediaRecorder」錄，確保每段都有完整檔頭、可獨立解碼。
// liveStarting 旗標＋「已在錄就不重啟」的檢查，避免 onstop 與 visibilitychange
// 同時觸發時建立兩個錄音器造成段落重複。
function recordSegment() {
  if (!liveRecording || liveStarting) return;
  if (liveRecorder && liveRecorder.state === "recording") return;
  liveStarting = true;
  const chunks = [];
  const mime = pickMime();
  const recorder = new MediaRecorder(liveStream, mime ? { mimeType: mime } : undefined);
  liveRecorder = recorder;
  const segStart = Math.floor((Date.now() - liveStartTime) / 1000);  // 本段在整場中的開始秒數
  recorder.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
  recorder.onerror = e => showError("錄音發生錯誤：" + ((e.error && e.error.message) || "未知原因"));
  recorder.onstop = () => {
    if (liveRecording) recordSegment();  // 先無縫接錄下一段，再上傳
    const blob = new Blob(chunks, { type: recorder.mimeType });
    if (blob.size > 0) uploadLiveChunk(blob, segStart);
  };
  recorder.start();
  liveStarting = false;
  // 第一段縮短到 12 秒：讓使用者快速看到第一句逐字稿，確認「真的有在聽」
  const secs = liveSegIndex === 0 ? Math.min(12, chunkSeconds) : chunkSeconds;
  liveSegIndex++;
  liveSegTimer = setTimeout(() => {
    if (recorder.state !== "inactive") recorder.stop();
  }, secs * 1000);
}

$("btnLiveStart").addEventListener("click", async () => {
  clearError();
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    showError("此瀏覽器無法使用麥克風：麥克風只在 https:// 加密連線（或 localhost）下可用，請確認網址是 https 開頭");
    return;
  }
  try {
    liveStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) { showError("無法取得麥克風權限：" + e.message + "（請到瀏覽器設定允許此網站使用麥克風）"); return; }
  try {
    liveSessionId = (await jsonOrThrow(await fetch("/api/live/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ translate_to: $("liveTranslate").value || null }),
    }))).session_id;
  } catch (e) { showError(e.message); return; }

  liveRecording = true;
  liveStartTime = Date.now();
  liveSegIndex = 0;
  liveSentCount = 0;
  liveSpeakers = {};
  acquireWakeLock();  // 保持螢幕常亮，避免手機鎖屏中斷錄音
  $("btnLiveStart").disabled = true;
  $("btnLiveStop").disabled = false;
  $("btnLiveRetry").style.display = "none";  // 開新一場，清掉上一場的重試入口
  $("liveTranscript").style.display = "block";
  $("liveTranscript").innerHTML =
    `<div class="cap-line" id="liveCaret"><span class="cap-time">[--:--]</span><span class="caret-block"></span></div>`;
  liveTickTimer = setInterval(liveTick, 1000);
  liveTick();
  recordSegment();
});

$("btnLiveStop").addEventListener("click", async () => {
  clearError();
  liveRecording = false;
  clearTimeout(liveSegTimer);
  clearInterval(liveTickTimer);
  releaseWakeLock();
  $("btnLiveStop").disabled = true;
  $("liveStatus").textContent = "整理最後一段錄音…";

  if (liveRecorder && liveRecorder.state !== "inactive") liveRecorder.stop();  // 觸發最後一段上傳
  liveStream.getTracks().forEach(t => t.stop());

  // 等所有音訊段上傳完成（含最後一段），最多等 3 分鐘
  const deadline = Date.now() + 180000;
  await new Promise(r => setTimeout(r, 300));  // 讓 onstop 先執行
  while (uploadsInFlight > 0 && Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 300));
  }
  const caret = $("liveCaret");
  if (caret) caret.remove();  // 收起打字游標

  await finishLiveSession();
});

// 結束彙整分析：失敗時「不」丟掉 session id，讓使用者可按「重試分析」再試，
// 不會因為一次額度/網路錯誤就白錄整場會議。
async function finishLiveSession() {
  if (!liveSessionId) return;
  $("btnLiveRetry").style.display = "none";
  $("btnLiveRetry").disabled = true;
  $("liveStatus").textContent = "AI 分析整場會議中…";
  analysisStartTime = Date.now();
  try {
    const result = await jsonOrThrow(await fetch(`/api/live/${liveSessionId}/finish`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        meeting_date: $("meetingDate").value || null,
        kind: $("meetingKind").value,
        features: selectedFeatures(),
        correct_typos: correctTypos(),
      }),
    }));
    $("liveStatus").textContent = "完成";
    liveSessionId = null;
    $("btnLiveStart").disabled = false;
    renderResult(result, result.transcript);
  } catch (e) {
    showError("分析失敗：" + e.message + "（逐字稿仍在，可按「重試分析」再試一次）");
    $("liveStatus").textContent = "分析失敗";
    $("btnLiveStart").disabled = false;  // 也可放棄、重新開始新的一場
    $("btnLiveRetry").style.display = "inline-flex";
    $("btnLiveRetry").disabled = false;
  }
}
$("btnLiveRetry").addEventListener("click", finishLiveSession);

/* ==================================================================
   10. 介面與資料工具：面板收縮、主題、設定選單、自訂詞彙、備份還原、PWA、觸覺回饋
   ================================================================== */
// ---- 面板收縮（點標題展開/收合，狀態記在瀏覽器）----
const COLLAPSE_KEY = "collapsedPanels";
function saveCollapsed() {
  const ids = [...document.querySelectorAll(".panel.collapsible.collapsed")].map(p => p.id);
  try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify(ids)); } catch (e) {}
}
(function initCollapse() {
  let saved = [];
  try { saved = JSON.parse(localStorage.getItem(COLLAPSE_KEY) || "[]"); } catch (e) {}
  document.querySelectorAll(".panel.collapsible").forEach(panel => {
    if (saved.includes(panel.id)) panel.classList.add("collapsed");
    panel.querySelector(".panel-head h2").addEventListener("click", () => {
      panel.classList.toggle("collapsed");
      saveCollapsed();
    });
  });
})();

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
        const u = await jsonOrThrow(await fetch("/api/usage"));
        const t = u.today || {};
        $("usageAnalysis").textContent = t.analysis || 0;
        $("usageAsk").textContent = t.ask || 0;
        $("usageLive").textContent = t.live_chunk || 0;
      } catch (err) {
        $("usageAnalysis").textContent = $("usageAsk").textContent = $("usageLive").textContent = "—";
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
        <button class="del-btn" data-i="${i}" title="刪除此詞彙" aria-label="刪除">✕</button>
      </div>`).join("")
    : `<p class="empty-note">尚無詞彙</p>`;
}

async function saveGlossary() {
  try {
    const r = await jsonOrThrow(await fetch("/api/glossary", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ terms: glosTerms }),
    }));
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
    const r = await jsonOrThrow(await fetch("/api/restore", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    }));
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
    glosTerms = (await jsonOrThrow(await fetch("/api/glossary"))).terms;
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
