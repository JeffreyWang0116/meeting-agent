/*
  會議助手 — 前端行為（app.js）
  畫面結構在 index.html、樣式在 style.css，三個檔案的功能分區順序一致。

  目錄：
    0. 版面路由（側欄切換 view）
    1. 共用基礎（API 認證、圖示、小工具）
    2. 全域初始化（日期、錄音種類、功能勾選、分頁）
    3. 逐字稿（連續文件式渲染、時間/引用句跳轉）
    4. 分析結果（摘要／會議重點／決議／代辦／行事曆／確認信）
    5. 任務庫
    6. 歷史會議
    7. 主動提醒與每日通知
    7.5 首頁儀表板（數字磚＋最近會議＋需要注意）
    8. 跨會議問答（RAG＋搜尋）
    9. 輸入路徑（純文字／檔案上傳／即時聆聽）
   10. 介面與資料工具（主題、設定、詞彙、備份、PWA）
*/
"use strict";
const $ = id => document.getElementById(id);
const PRIORITY_ZH = { high: "高", medium: "中", low: "低" };
let chunkSeconds = 45;

/* ==================================================================
   0. 版面路由：側欄一次只顯示一個 view
   ------------------------------------------------------------------
   所有 view 的資料在載入時就一起抓（refreshTasks/Meetings/Reminders），
   切換只是顯示/隱藏，不重新請求，所以切分頁沒有等待感。
   ================================================================== */
// 結果頁的雙欄斷點，和 style.css 的 @media (max-width: 1180px) 對齊
const WIDE = window.matchMedia("(min-width: 1181px)");
const VIEW_TITLES = {
  home: "首頁", new: "新會議", result: "分析結果",
  reminder: "主動提醒", ask: "詢問會議", task: "任務庫", meeting: "歷史會議",
};
function showView(name) {
  if (!VIEW_TITLES[name]) name = "home";
  // 結果頁在跑過一次分析前是空的，別讓網址列直接跳進去
  if (name === "result" && $("navResult").hidden) name = "home";
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("active", v.id === `view-${name}`));
  document.querySelectorAll(".nav-item").forEach(b => {
    const on = b.dataset.view === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-current", on ? "page" : "false");
  });
  $("viewTitle").textContent = VIEW_TITLES[name];
  if (location.hash.slice(1) !== name) history.replaceState(null, "", `#${name}`);
  window.scrollTo(0, 0);
}
// 側欄、數字磚、面板上的「看全部」共用同一個屬性，不必各自綁事件
document.addEventListener("click", e => {
  const el = e.target.closest("[data-view]");
  if (el) showView(el.dataset.view);
});
window.addEventListener("hashchange", () => showView(location.hash.slice(1)));
showView(location.hash.slice(1) || "home");

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

// ---- 圖示：Lucide，全部收在 /static/icons.svg 雪碧圖（授權說明見該檔開頭） ----
// 用 <use> 引用 symbol：路徑資料只存一份，樣式（大小、stroke-width、顏色）
// 由 .i / .i-sm / .i-lg 在 CSS 決定，繼承進 shadow tree
function icon(name, cls) {
  return `<svg class="${cls || "i"}" aria-hidden="true"><use href="/static/icons.svg#${name}"/></svg>`;
}

// ---- 骨架載入 ----
// 資料還沒回來時先擺出版面輪廓，取代「尚無資料」——後者在載入中是騙人的。
// 每支 refresh 都要負責把骨架收掉（成功換成資料、失敗換成重試），不能讓它一直閃。
const skelLine = w => `<span class="skel" style="width:${w}"></span>`;
const skelBlocks = n => `<div class="skel-list">${
  Array.from({ length: n }, () => `<div class="skel-block">${skelLine("58%")}${skelLine("32%")}</div>`).join("")
}</div>`;
const skelRows = (n, cols) => Array.from({ length: n }, () =>
  `<tr class="skel-row">${Array.from({ length: cols }, () => `<td>${skelLine("72%")}</td>`).join("")}</tr>`).join("");

let tasksLoaded = false, meetingsLoaded = false, remindersLoaded = false;

// ---- 清單分頁 ----
// 任務、會議、提醒本來就是一次 fetch 全部回來，所以分頁純在前端切，不動 API。
// 只有超過每頁筆數才會出現頁碼列——資料還少的時候，使用者完全不會看到這個功能。
// 三個清單一律 10 筆一頁：單筆高度雖然不同，但一致的節奏比各自最佳化好預期，
// 也讓分頁在資料量還不多的時候就先出現。要個別調整改這裡即可。
const PAGE_SIZE = { tasks: 10, meetings: 10, reminders: 10 };
const pageNo = { tasks: 1, meetings: 1, reminders: 1 };

function paginate(kind, items) {
  const size = PAGE_SIZE[kind];
  const pages = Math.max(1, Math.ceil(items.length / size));
  if (pageNo[kind] > pages) pageNo[kind] = pages;  // 刪到這一頁空了就自動往前收
  const from = (pageNo[kind] - 1) * size;
  return { items: items.slice(from, from + size), pages, current: pageNo[kind], total: items.length, from };
}

function renderPager(kind, p) {
  const box = $(`${kind}Pager`);
  if (p.pages <= 1) { box.innerHTML = ""; return; }
  box.innerHTML =
    `<button class="ghost" data-page="prev" ${p.current === 1 ? "disabled" : ""}>` +
      `${icon("chevron-left", "i-sm")}上一頁</button>` +
    `<span class="pager-info">${p.from + 1}–${p.from + p.items.length} / 共 ${p.total} 筆` +
      `<span class="pager-sep">·</span>第 ${p.current} / ${p.pages} 頁</span>` +
    `<button class="ghost" data-page="next" ${p.current === p.pages ? "disabled" : ""}>` +
      `下一頁${icon("chevron-right", "i-sm")}</button>`;
}

document.addEventListener("click", e => {
  const btn = e.target.closest(".pager [data-page]");
  if (!btn) return;
  const kind = btn.closest(".pager").dataset.pager;
  pageNo[kind] += btn.dataset.page === "next" ? 1 : -1;
  ({ tasks: renderTasks, meetings: renderMeetings, reminders: renderReminders })[kind]();
  // 翻頁後要從新一頁的開頭看起，不然會停在上一頁的位置
  $(`${kind}Pager`).closest(".panel").scrollIntoView({ behavior: "smooth", block: "start" });
});

function loadFail(kind) {
  return `<p class="load-fail">${icon("circle-alert", "i-sm")}載不到資料<button class="ghost" data-retry="${kind}">重試</button></p>`;
}
document.addEventListener("click", e => {
  const btn = e.target.closest("[data-retry]");
  if (!btn) return;
  ({ tasks: refreshTasks, meetings: refreshMeetings, reminders: refreshReminders })[btn.dataset.retry]?.();
});

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function showError(msg) {
  const b = $("errorBanner");
  b.classList.remove("notice");
  b.innerHTML = icon("circle-alert") + `<span>${esc(msg)}</span>`;
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
  if (!resp.ok) {
    // status 帶在 Error 上：呼叫端要能分辨「這筆資料不存在」(404) 與「暫時性故障」
    // (502/429)，兩者的處置完全不同
    const err = new Error(body.detail || `伺服器錯誤（${resp.status}）`);
    err.status = resp.status;
    throw err;
  }
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

// AI 辨識講者姓名：把講者A/B/C 換成真實姓名。預設關閉——台語等語者辨識不穩的
// 錄音容易對錯，猜錯的名字比代號更糟；想要姓名時再手動勾。記住選擇。
(function () {
  if (localStorage.getItem("nameSpeakers") === "1") $("featNameSpeakers").checked = true;
  $("featNameSpeakers").addEventListener("change", () =>
    localStorage.setItem("nameSpeakers", $("featNameSpeakers").checked ? "1" : "0"));
})();
function nameSpeakers() { return $("featNameSpeakers").checked; }

// 即時聆聽同時收系統／耳機音源：線上會議戴耳機時麥克風收不到對方，勾了才會把對方
// 的聲音一起錄。預設關閉（多一次權限、且僅桌機支援），記住選擇。
(function () {
  if (localStorage.getItem("liveSystemAudio") === "1") $("liveSystemAudio").checked = true;
  const sync = (interactive) => {
    localStorage.setItem("liveSystemAudio", $("liveSystemAudio").checked ? "1" : "0");
    $("liveSysSourceRow").style.display = $("liveSystemAudio").checked ? "" : "none";
    // interactive＝使用者剛手動勾選，才可為了取得裝置名稱去要一次麥克風權限；
    // 頁面載入時的還原不帶 interactive，避免一開頁就跳權限。
    if ($("liveSystemAudio").checked) populateSysSources(interactive);
  };
  $("liveSystemAudio").addEventListener("change", () => sync(true));
  // 點開下拉時也解鎖裝置名稱（此時多半已授權過，通常不會再跳權限）
  $("liveSysSource").addEventListener("focus", () => populateSysSources(true));
  sync(false);  // 進頁面時依記住的勾選狀態決定要不要展開來源選單
})();
function wantSystemAudio() { return $("liveSystemAudio").checked; }
function sysSourceValue() { return $("liveSysSource").value || "display"; }

// 偵測可直接擷取的「回放輸入裝置」（Windows 立體聲混音、虛擬音效線等）。抓得到就能
// 用 getUserMedia 直接錄耳機音源、免跳分享視窗；抓不到就只留「分享畫面擷取」。
// 裝置標籤要授權過麥克風才看得到，所以標籤空白時先要一次麥克風權限再重新列舉。
const LOOPBACK_RE = /stereo mix|立體聲混音|立体声混音|what ?u ?hear|loopback|cable|voicemeeter|virtual|混音/i;
async function populateSysSources(interactive) {
  const sel = $("liveSysSource"), hint = $("liveSysHint");
  if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
    hint.textContent = "此瀏覽器無法列舉音訊裝置，將使用分享畫面擷取。";
    return;
  }
  const remembered = localStorage.getItem("liveSysSource") || "display";
  try {
    let inputs = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === "audioinput");
    if (interactive && inputs.length && !inputs[0].label) {  // 沒標籤＝還沒授權過，使用者主動操作時才要一次麥克風權限解鎖名稱
      try { (await navigator.mediaDevices.getUserMedia({ audio: true })).getTracks().forEach(t => t.stop()); } catch (e) {}
      inputs = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === "audioinput");
    }
    if (inputs.length && !inputs[0].label) {  // 仍無標籤（尚未授權）：先只放分享畫面，等使用者互動再補
      hint.textContent = "按一下這個選單或開始聆聽授權麥克風後，才會列出可直接擷取的音源裝置。";
      return;
    }
    const loop = inputs.filter(d => LOOPBACK_RE.test(d.label));
    // 重建選項：偵測到的回放裝置在前、分享畫面永遠墊底當備援
    sel.innerHTML = "";
    for (const d of loop) {
      sel.appendChild(new Option(`🎧 ${d.label}（直接擷取，免分享視窗）`, d.deviceId));
    }
    sel.appendChild(new Option("分享畫面擷取（每次會跳分享視窗，相容性最高）", "display"));
    // 還原上次選擇；找不到（裝置變動）就退回第一個回放裝置、再退回分享畫面
    sel.value = remembered;
    if (sel.value !== remembered) sel.value = loop.length ? loop[0].deviceId : "display";
    hint.textContent = loop.length
      ? "已偵測到可直接擷取的音源裝置，選它就不用每次分享畫面。"
      : "找不到可直接擷取的裝置。在 Windows 音效設定 → 錄製 → 啟用「立體聲混音」後重新整理，即可直接選用、免分享畫面。";
  } catch (e) {
    hint.textContent = "列舉裝置失敗，將使用分享畫面擷取：" + e.message;
  }
}
$("liveSysSource").addEventListener("change", () =>
  localStorage.setItem("liveSysSource", $("liveSysSource").value));

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
  // 逐字稿容器自己會捲（結果頁右欄、歷史會議詳情都是）。直接算它的 scrollTop，
  // 不用 scrollIntoView——後者會連整頁一起捲走，把左邊剛點的那張卡片捲不見
  if (container.scrollHeight > container.clientHeight + 1) {
    const delta = target.getBoundingClientRect().top - container.getBoundingClientRect().top;
    container.scrollTo({
      top: container.scrollTop + delta - (container.clientHeight - target.offsetHeight) / 2,
      behavior: "smooth",
    });
  } else {
    target.scrollIntoView({ behavior: "smooth", block: "center" });
  }
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

// AI 分析要跑十幾秒到一分鐘。與其讓人盯著一顆 disabled 的按鈕，不如先把報告的
// 版面輪廓擺出來：既回饋「系統在做事」，也預告等一下會拿到哪些區塊。
// renderResult 會把每一格覆蓋掉，所以這裡填什麼都不會殘留。
function showResultSkeleton() {
  if ($("result").classList.contains("is-loading")) return;  // 輪詢會重複呼叫，別一直把畫面捲回頂端
  const lines = n => Array.from({ length: n }, (_, i) => skelLine(`${94 - i * 12}%`)).join("");
  $("rTitle").innerHTML = skelLine("46%");
  $("rMeta").innerHTML = `${skelLine("92px")}${skelLine("64px")}${skelLine("120px")}`;
  $("rSummary").innerHTML = lines(3);
  $("rHighlights").innerHTML = skelBlocks(3);
  $("rDecisions").innerHTML = skelBlocks(2);
  $("rTodos").innerHTML = skelBlocks(3);
  $("rPending").innerHTML = skelBlocks(1);
  $("rEvents").innerHTML = skelBlocks(1);
  $("rDraft").innerHTML = lines(4);
  ["hSummary", "hHighlights", "hDecisions", "hTodos"].forEach(id => ($(id).style.display = "flex"));
  $("rSummary").style.display = "block";
  $("rHighlights").style.display = "flex";
  $("rDecisions").style.display = "flex";
  $("rTodos").style.display = "flex";
  $("rSummaryTrans").style.display = "none";
  $("rTransSec").style.display = "none";
  $("rCorrSec").style.display = "none";
  $("result").classList.add("is-loading");
  $("result").style.display = "flex";
  $("navResult").hidden = false;
  showView("result");
}

// 分析失敗：骨架整個收掉退回「新會議」。上一份結果這時也已經過期了，一併清掉，
// 免得使用者以為那是這次跑出來的
function hideResultSkeleton() {
  if (!$("result").classList.contains("is-loading")) return;
  $("result").classList.remove("is-loading");
  $("result").style.display = "none";
  $("navResult").hidden = true;
  showView("new");
}

function renderResult(result, transcript) {
  $("result").classList.remove("is-loading");
  // 後端校正過的話，result.transcript 才是最終版本（傳進來的可能是校正前的）
  currentTranscript = (result.transcript || transcript || "").trim();
  $("rTransSec").style.display = currentTranscript ? "block" : "none";
  // 雙欄版面下逐字稿是常駐對照欄，預設攤開；單欄（窄螢幕）才收起來免得洗版
  $("rTransSec").open = WIDE.matches;
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
    ? a.pending_items.map(p => `<div class="pending-item">${icon("circle-help")}<span>${esc(p.topic)}${p.reason ? `<span class="reason">${esc(p.reason)}</span>` : ""}</span></div>`).join("")
    : `<p class="empty-note">無</p>`;

  $("icsLink").href = `/api/meetings/${encodeURIComponent(result.meeting_id)}/events.ics`;
  const events = result.notifications.calendar_events || [];
  $("rEvents").innerHTML = events.length
    ? events.map(e => `<div class="event-item">${icon("calendar")}<span><span class="when">${esc(e.start.date)}</span><b>${esc(e.summary)}</b><span class="desc">${esc(e.description).replace(/\n/g, " · ")}</span></span></div>`).join("")
    : `<p class="empty-note">沒有含期限的代辦，未產生行事曆事件</p>`;

  $("rDraft").textContent = result.notifications.email_draft || "";
  draftSubject = result.notifications.email_subject || "";
  $("result").style.display = "flex";
  $("navResult").hidden = false;
  showView("result");
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

// ---- 確認信一鍵開信 ----
// 收件人刻意留空（出席者名單在信件內文裡，姓名對不到 email），使用者在信件視窗自己選。
// 主旨與內文則用網址參數帶進去。中文經 encodeURIComponent 後一個字會膨脹成 9 個字元，
// 一份含十幾項代辦的草稿很容易把網址撐到上萬字元，所以兩條路徑各有長度上限：
// mailto: 交給作業系統的郵件軟體處理，上限最低（Windows 實測約 2000 字元就會被截斷）；
// Gmail 網頁版寬鬆得多。超過就改走「複製全文到剪貼簿 + 只帶主旨開信」——
// 讓使用者按一下貼上，好過寄出一封內容被砍一半的信。
const MAILTO_URL_LIMIT = 1800;
const GMAIL_URL_LIMIT = 7000;

let draftSubject = "";  // 由 renderResult 從 notifications.email_subject 帶入

function draftSubjectAndBody() {
  const draft = $("rDraft").textContent || "";
  const lines = draft.split("\n");
  // 主旨會另外填進信件的主旨欄，內文再放一次會重複；順手吃掉它後面的空行
  let subject = draftSubject;
  if (lines[0] && lines[0].startsWith("主旨：")) {
    if (!subject) subject = lines[0].slice(3);  // 舊會議的草稿沒有 email_subject 欄位
    lines.shift();
    while (lines.length && !lines[0].trim()) lines.shift();
  }
  return { subject: subject || "會議紀錄確認", body: lines.join("\n"), draft };
}

async function openCompose(via) {
  const { subject, body, draft } = draftSubjectAndBody();
  if (!draft.trim()) return;
  const build = b => via === "gmail"
    ? `https://mail.google.com/mail/?view=cm&fs=1&su=${encodeURIComponent(subject)}&body=${encodeURIComponent(b)}`
    : `mailto:?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(b)}`;

  let url = build(body);
  if (url.length > (via === "gmail" ? GMAIL_URL_LIMIT : MAILTO_URL_LIMIT)) {
    url = build("");
    try {
      await navigator.clipboard.writeText(draft);
      showNotice("信件內容太長，無法帶進網址：已複製全文到剪貼簿，請在信件裡直接貼上。");
    } catch {
      showError("信件內容太長，無法帶進網址：請改按「複製」再自行貼到信件裡。");
      return;
    }
  }
  // mailto 交給系統處理（不會真的離開頁面）；Gmail 是網頁，開新分頁才不會蓋掉分析結果
  if (via === "gmail") window.open(url, "_blank", "noopener");
  else window.location.href = url;
}

$("btnGmailDraft").addEventListener("click", () => openCompose("gmail"));
$("btnMailtoDraft").addEventListener("click", () => openCompose("mailto"));

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
          <button class="del-btn cancel-edit" title="取消" aria-label="取消">${icon("x", "i-sm")}</button>
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
            ${icon("square-pen", "i-sm")}
          </button>
          <button class="del-btn" data-id="${esc(t.id)}" title="刪除此任務" aria-label="刪除">${icon("x", "i-sm")}</button>
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
  const p = paginate("tasks", rows);
  $("taskRows").innerHTML = p.items.length
    ? p.items.map(taskRowHtml).join("")
    : `<tr><td colspan="7" class="empty-note">${allTasks.length ? "沒有符合條件的任務" : "尚無任務"}</td></tr>`;
  renderPager("tasks", p);
  renderHome();
}

async function refreshTasks() {
  try {
    allTasks = (await jsonOrThrow(await fetch("/api/tasks"))).tasks;
    tasksLoaded = true;
    renderTasks();
  } catch (e) {
    tasksLoaded = true;  // 骨架不能一直閃：載不到就明講，並留一個重試入口
    $("taskRows").innerHTML = `<tr><td colspan="7">${loadFail("tasks")}</td></tr>`;
    renderHome();
  }
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

// 換了搜尋條件就回到第一頁，否則會停在一個超出新結果筆數的頁碼上
const backToFirstTaskPage = () => { pageNo.tasks = 1; renderTasks(); };
$("taskSearch").addEventListener("input", backToFirstTaskPage);
$("taskFilter").addEventListener("change", backToFirstTaskPage);
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
      <h4>詞彙統一替換</h4>
      <div class="term-tool">
        <div class="term-replace">
          <input type="text" class="term-from" placeholder="原詞（聽錯的，如：涵式）">
          <span class="term-arrow">→</span>
          <input type="text" class="term-to" placeholder="改成（如：函式）">
          <button class="ghost term-apply" data-id="${esc(id)}">替換</button>
        </div>
        <div class="term-replace">
          <span class="term-range-lbl">限定時間段（選填）</span>
          <input type="text" class="term-start" placeholder="起 如 12:30">
          <span class="term-arrow">–</span>
          <input type="text" class="term-end" placeholder="迄 如 15:00">
          <span class="glos-note">留空＝整份逐字稿</span>
        </div>
        <label class="chk term-glos"><input type="checkbox" checked> 一併加入詞彙表，之後轉錄不再聽錯</label>
      </div>
      <h4>完整逐字稿 <button class="ghost copy-detail" data-id="${esc(id)}">複製全文</button></h4>
      <div class="detail-transcript" id="dTranscriptView"></div>
      <div class="detail-actions">
        <button class="ghost edit-detail" data-id="${esc(id)}">
          ${icon("square-pen", "i-sm")}
          編輯
        </button>
        <button class="ghost reanalyze-detail" data-id="${esc(id)}">
          ${icon("refresh-cw", "i-sm")}
          重新分析
        </button>
        <button class="ghost share-detail" data-id="${esc(id)}">
          ${icon("share-2", "i-sm")}
          分享
        </button>
        <a class="ghost btn-link" href="/api/meetings/${esc(id)}/events.ics" download>加入行事曆</a>
      </div>
    </div>`;
}

let activeTag = "";  // 歷史會議標籤篩選（"" = 全部）
let tagsExpanded = false;  // AI 每場會議都自己取標籤，久了會很雜——預設只顯示最常用的幾個
const TAG_FILTER_LIMIT = 12;

function renderTagFilter() {
  const counts = new Map();
  allMeetings.forEach(m => {
    if (m.kind) counts.set(m.kind, (counts.get(m.kind) || 0) + 1);
    (m.tags || []).forEach(t => counts.set(t, (counts.get(t) || 0) + 1));
  });
  const bar = $("tagFilter");
  if (!counts.size) { bar.style.display = "none"; return; }
  if (activeTag && !counts.has(activeTag)) activeTag = "";
  bar.style.display = "flex";

  const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([t]) => t);
  const overflow = sorted.length - TAG_FILTER_LIMIT;
  let visible = overflow <= 0 || tagsExpanded ? sorted : sorted.slice(0, TAG_FILTER_LIMIT);
  // 目前選中的標籤若被截掉了也要顯示，不然使用者會看不到自己選的是哪個
  if (activeTag && !visible.includes(activeTag)) visible = [...visible, activeTag];

  bar.innerHTML = [`<span class="tag-chip ${activeTag ? "" : "active"}" data-tag="">全部</span>`]
    .concat(visible.map(t =>
      `<span class="tag-chip ${t === activeTag ? "active" : ""}" data-tag="${esc(t)}">${esc(t)}</span>`))
    .concat(overflow > 0
      ? [`<span class="tag-chip tag-more" data-more="1">${tagsExpanded ? "收合" : `更多 +${overflow}`}</span>`]
      : [])
    .join("");
}

const filteredMeetings = () => allMeetings.filter(m =>
  !activeTag || m.kind === activeTag || (m.tags || []).includes(activeTag));

// 從首頁或跨會議搜尋跳過來的會議，可能不在目前這一頁，甚至被標籤篩掉了。
// 展開之前先把畫面帶到它所在的位置，不然點了會像沒反應。
function focusMeetingPage(id) {
  if (!allMeetings.some(m => m.id === id)) return;
  if (!filteredMeetings().some(m => m.id === id)) activeTag = "";
  const at = filteredMeetings().findIndex(m => m.id === id);
  if (at >= 0) pageNo.meetings = Math.floor(at / PAGE_SIZE.meetings) + 1;
}

function renderMeetings() {
  renderTagFilter();
  const p = paginate("meetings", filteredMeetings());
  $("meetingRows").innerHTML = p.items.length
    ? p.items.map(m => `<div class="meeting-item ${m.id === expandedMeetingId ? "expanded" : ""}">
          <span class="meeting-info">
            <b>${esc(m.meeting.title)}</b>
            <span class="meeting-meta mono">${esc(m.meeting.date)}${m.kind ? ` · ${esc(m.kind)}` : ""} · ${esc(m.id)}</span>
            ${(m.tags || []).length ? `<span class="meeting-tags">${m.tags.map(t => `<span class="mtag">${esc(t)}</span>`).join("")}</span>` : ""}
          </span>
          <span class="meeting-ops">
            <button class="ghost view-meeting" data-id="${esc(m.id)}">${m.id === expandedMeetingId ? "收合" : "查閱"}</button>
            <a class="ghost btn-link" href="/api/meetings/${esc(m.id)}/report.md" download>下載</a>
            <button class="del-btn del-meeting" data-id="${esc(m.id)}" title="刪除此會議與其任務" aria-label="刪除">${icon("x", "i-sm")}</button>
          </span>
        </div>` + (m.id === expandedMeetingId ? meetingDetailHtml(m.id) : "")).join("")
    : `<p class="empty-note">${allMeetings.length ? "沒有符合此標籤的會議" : "尚無會議紀錄"}</p>`;
  renderPager("meetings", p);
  // 逐字稿要等 DOM 建立後渲染
  const view = document.getElementById("dTranscriptView");
  const d = meetingDetailCache[expandedMeetingId];
  if (view && d) {
    if (d.transcript) renderChat(view, d.transcript);
    else view.innerHTML = `<p class="empty-note">此會議沒有存逐字稿全文</p>`;
  }
  renderHome();
}

async function refreshMeetings() {
  try {
    allMeetings = (await jsonOrThrow(await fetch("/api/meetings"))).meetings;
    meetingsLoaded = true;
    renderMeetings();
    renderAskScope();
  } catch (e) {
    meetingsLoaded = true;
    $("meetingRows").innerHTML = loadFail("meetings");
    renderHome();
  }
}

async function openMeetingDetail(id) {
  focusMeetingPage(id);
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
        body: JSON.stringify({ correct_typos: correct, name_speakers: nameSpeakers() }),
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

  const termBtn = e.target.closest(".term-apply");
  if (termBtn) {
    const id = termBtn.dataset.id;
    const wrap = termBtn.closest(".term-tool");
    const from = wrap.querySelector(".term-from").value.trim();
    const to = wrap.querySelector(".term-to").value;  // 不 trim：允許留空＝把該詞刪掉
    const start = wrap.querySelector(".term-start").value.trim();
    const end = wrap.querySelector(".term-end").value.trim();
    const addGlos = wrap.querySelector(".term-glos input").checked;
    if (!from) { showError("請先輸入要替換的原詞"); return; }
    if (from === to.trim()) { showError("原詞與新詞相同，無需替換"); return; }
    const rangeNote = (start || end) ? `（時間段 ${start || "開頭"}–${end || "結尾"}）` : "";
    termBtn.disabled = true; termBtn.textContent = "替換中…";
    try {
      const r = await jsonOrThrow(await fetch(`/api/meetings/${id}/replace-term`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ old: from, new: to, add_to_glossary: addGlos, start: start || null, end: end || null }),
      }));
      if (r.replaced === 0) {
        showNotice(`${rangeNote ? "指定時間段裡" : "逐字稿裡"}找不到「${from}」，沒有任何替換。`);
        termBtn.disabled = false; termBtn.textContent = "替換";
        return;
      }
      meetingDetailCache[id] = r.meeting;
      showNotice(`已把「${from}」替換成「${to || "（刪除）"}」共 ${r.replaced} 處${rangeNote}` +
        (r.glossary_added ? "，並已加入詞彙表" : "") + "。");
      renderMeetings();
    } catch (err) {
      showError("詞彙替換失敗：" + err.message);
      termBtn.disabled = false; termBtn.textContent = "替換";
    }
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
      rememberSpeaker(newName);  // 記進名冊，下次辨識講者時姓名寫法就有依據
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

// 標籤篩選
$("tagFilter").addEventListener("click", e => {
  if (e.target.closest("[data-more]")) { tagsExpanded = !tagsExpanded; renderTagFilter(); return; }
  const chip = e.target.closest(".tag-chip");
  if (!chip) return;
  activeTag = chip.dataset.tag;
  pageNo.meetings = 1;  // 換標籤等於換一份清單，從第一頁看起
  renderMeetings();
});

/* ==================================================================
   7.5 首頁儀表板
   ------------------------------------------------------------------
   不另外打 API，把 refreshTasks/refreshMeetings/refreshReminders 已經
   抓回來的資料濃縮成四個數字＋兩張短清單，讓人一進站就知道現況。
   ================================================================== */
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
       <button class="del-btn del-ask" title="刪除這則問答" aria-label="刪除">${icon("x", "i-sm")}</button>
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
  showView("meeting");
  openMeetingDetail(hit.dataset.id);
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
  showResultSkeleton();
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
        name_speakers: nameSpeakers(),
      }),
    }));
    renderResult(result, $("textInput").value);
  } catch (e) { hideResultSkeleton(); showError(e.message); }
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
    if (nameSpeakers()) form.append("name_speakers", "true");
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
      // 轉錄階段的進度條與逐字稿在「新會議」畫面，看得到才有意義；
      // 進到分析階段才切去結果頁擺骨架
      if (job.status === "analyzing") showResultSkeleton();
      if (job.status === "done") { renderResult(job.result, job.transcript); break; }
      if (job.status === "error") throw new Error(job.error || "轉錄失敗");
    }
  } catch (e) { hideResultSkeleton(); showError(e.message); $("fileStatus").textContent = "失敗"; }
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
// liveStream 是實際交給 MediaRecorder 錄的那條軌：只錄麥克風時就是麥克風串流本身；
// 若同時收系統／耳機音源，則是「麥克風＋系統音源」混音後的輸出。liveMicStream／
// liveSysStream 保留原始來源，結束時要各自關掉裝置；liveMixCtx 是負責混音的 AudioContext。
let liveStream = null, liveRecorder = null, liveSessionId = null;
let liveMicStream = null, liveSysStream = null, liveMixCtx = null;
let liveRecording = false, liveSegTimer = null, uploadsInFlight = 0, liveStartTime = null, liveTickTimer = null;
let liveSegIndex = 0, liveSentCount = 0, liveWakeLock = null, liveStarting = false;

// 取得要錄的串流。withSystemAudio 為真時，額外抓耳機／系統音源（對方的聲音），
// 和麥克風混成一條軌一起錄。音源來源由 sysSourceValue() 決定：
//   deviceId → 直接用 getUserMedia 錄該回放裝置（立體聲混音等），免跳分享視窗；
//   "display" → 用 getDisplayMedia 分享畫面擷取（相容性最高，但每次會跳分享視窗）。
async function buildLiveStream(withSystemAudio) {
  try {
    liveMicStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    throw new Error("無法取得麥克風權限：" + e.message + "（請到瀏覽器設定允許此網站使用麥克風）");
  }
  if (!withSystemAudio) return liveMicStream;

  liveSysStream = await acquireSystemAudio(sysSourceValue());

  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) throw new Error("此瀏覽器不支援 Web Audio，無法混合系統音源");
  liveMixCtx = new Ctx();
  if (liveMixCtx.state === "suspended") liveMixCtx.resume().catch(() => {});
  const dest = liveMixCtx.createMediaStreamDestination();
  liveMixCtx.createMediaStreamSource(liveMicStream).connect(dest);
  liveMixCtx.createMediaStreamSource(liveSysStream).connect(dest);
  return dest.stream;
}

// 依所選來源取得耳機／系統音源串流；接上時提示、失敗時丟出可讀的錯誤。
async function acquireSystemAudio(source) {
  // 直接擷取回放裝置（立體聲混音／虛擬音效線）——關掉麥克風用的回音消除等處理，避免破壞系統音源
  if (source && source !== "display") {
    let sys;
    try {
      sys = await navigator.mediaDevices.getUserMedia({
        audio: { deviceId: { exact: source }, echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      });
    } catch (e) {
      throw new Error("無法開啟所選的系統音源裝置：" + e.message + "。請改選其他來源，或在下拉選單改用「分享畫面擷取」");
    }
    const t = sys.getAudioTracks()[0];
    console.log("[live] 已接上系統音源裝置：", t && t.label);
    showNotice("已接上耳機／系統音源（" + ((t && t.label) || "所選裝置") + "），對方的聲音會一起錄進逐字稿。");
    return sys;
  }

  // 分享畫面擷取
  if (!navigator.mediaDevices.getDisplayMedia) {
    throw new Error("此瀏覽器不支援分享畫面擷取（此功能僅桌機版 Chrome／Edge 可用），請取消勾選「同時收錄耳機／系統音源」");
  }
  let sys;
  try {  // 分享對話框一定要挑一個畫面來源才會給音訊，所以連 video 一起要。
    sys = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
  } catch (e) {
    throw new Error("未取得系統音源分享（已取消或被拒）：" + e.message);
  }
  const sysAudio = sys.getAudioTracks()[0];
  if (!sysAudio) {
    sys.getTracks().forEach(t => t.stop());
    throw new Error("這次分享沒有帶到聲音。桌面 App 開會請選「整個螢幕」、會議在瀏覽器分頁請選該「分頁」，並務必勾選「分享系統音訊／分頁音訊」再試一次");
  }
  // 關鍵：不要 stop 掉畫面軌！系統音源的擷取綁在這個螢幕分享 session 上，
  // 一旦停掉畫面，聲音會跟著斷（症狀就是「只錄到麥克風」）。改成把畫面「停用」
  // ——產生黑畫面、幾乎不吃資源，但 session 保持存活，聲音才會持續進來。
  sys.getVideoTracks().forEach(t => { t.enabled = false; });
  console.log("[live] 已接上系統音源（分享畫面）：", sysAudio.label || "(未命名)", "muted=", sysAudio.muted);
  showNotice("已接上系統／耳機音源，對方的聲音會一起錄進逐字稿。請保持螢幕分享開著，不要按瀏覽器的「停止分享」。");
  sysAudio.addEventListener("ended", () => {  // 使用者按「停止分享」時提醒接下來只剩麥克風
    if (liveRecording) showNotice("螢幕／系統音源分享已停止，接下來只會錄到麥克風。");
  });
  return sys;
}

// 關掉聆聽用到的所有音訊來源與混音器（麥克風、系統音源、混音 AudioContext）
function releaseLiveStreams() {
  for (const s of [liveMicStream, liveSysStream, liveStream]) {
    if (s) s.getTracks().forEach(t => t.stop());
  }
  if (liveMixCtx) liveMixCtx.close().catch(() => {});
  liveMicStream = liveSysStream = liveStream = liveMixCtx = null;
}

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
  const clock = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  const pending = uploadsInFlight > 0 ? `，${uploadsInFlight} 段辨識中…` : "";
  const sent = liveSentCount > 0 ? `已送出 ${liveSentCount} 段${pending}` : `第一段約 ${Math.min(12, chunkSeconds)} 秒後送出`;
  $("liveStatus").innerHTML = `<span class="rec-dot"></span>聆聽中 ${clock}（${sent}）`;
  if (!$("liveStage").hidden) {
    $("stageTime").textContent = clock;
    $("stageNote").innerHTML = `<span class="rec-dot"></span>${sent}`;
  }
}

// ---- 聆聽沉浸畫面：聲控光球＋計時＋即時字幕 ----
// 音量分析共用 liveStream（MediaRecorder 錄的同一份），不另外開麥克風：
// 手機上開第二份串流可能跟錄音搶裝置，也會多跳一次權限。
let orb = null, orbRaf = 0, audioCtx = null, analyser = null, analyserData = null;
let transcriptHome = null;  // 逐字稿節點原本的位置，收合時要放回去

function initStageAudio() {
  if (analyser || !liveStream) return;
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return;
  try {
    audioCtx = new Ctx();
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 512;
    analyser.smoothingTimeConstant = 0.3;
    // 只接到 analyser、刻意不接 destination——接上輸出等於把麥克風擴音出來，會回授
    audioCtx.createMediaStreamSource(liveStream).connect(analyser);
    analyserData = new Uint8Array(analyser.frequencyBinCount);
  } catch (e) {
    audioCtx = null; analyser = null;  // 分析失敗不影響錄音，光球退化成只自轉
  }
}

function releaseStageAudio() {
  if (audioCtx) audioCtx.close().catch(() => {});
  audioCtx = null; analyser = null; analyserData = null;
}

function pumpLevel() {
  orbRaf = requestAnimationFrame(pumpLevel);
  if (!orb || !analyser) return;
  analyser.getByteFrequencyData(analyserData);
  let sum = 0;
  for (let i = 0; i < analyserData.length; i++) {
    const v = analyserData[i] / 255;
    sum += v * v;
  }
  orb.setLevel(Math.sqrt(sum / analyserData.length) * 4.5);  // RMS 比平均值更貼近人聲強弱
}

function openStage() {
  $("liveStage").hidden = false;
  $("btnLiveStage").style.display = "none";

  // 把逐字稿「整個節點」搬進沉浸畫面（不是複製），現有字幕附加邏輯才不用改
  const box = $("liveTranscript");
  if (!transcriptHome) transcriptHome = { parent: box.parentNode, next: box.nextSibling };
  $("stageSlot").appendChild(box);

  const holder = $("stageOrb");
  orb = window.createVoiceOrb ? window.createVoiceOrb(holder) : null;
  holder.classList.toggle("no-webgl", !orb);  // 沒有 WebGL 就退回 CSS 呼吸光暈

  initStageAudio();
  if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
  cancelAnimationFrame(orbRaf);
  pumpLevel();
  liveTick();
}

// 只收合畫面，錄音與上傳完全不受影響
function closeStage() {
  if ($("liveStage").hidden) return;
  $("liveStage").hidden = true;
  cancelAnimationFrame(orbRaf);
  orbRaf = 0;
  if (orb) { orb.destroy(); orb = null; }
  const box = $("liveTranscript");
  if (transcriptHome) {
    transcriptHome.parent.insertBefore(box, transcriptHome.next);
    transcriptHome = null;
  }
}

$("btnStageStop").addEventListener("click", () => $("btnLiveStop").click());
$("btnStageClose").addEventListener("click", () => {
  closeStage();
  if (liveRecording) $("btnLiveStage").style.display = "inline-flex";
});
$("btnLiveStage").addEventListener("click", openStage);

// 即時字幕流：逐段附加逐字稿行（後端回傳的文字已帶整場時間戳），末端保留打字游標。
// liveSpeakers 讓同一位講者在整場聆聽中維持同色。
let liveSpeakers = {};
// 逐字稿原文（非 HTML）。伺服器重啟會讓聆聽 session 連同它累積的逐字稿一起消失，
// 這份瀏覽器端的副本是那時唯一還救得回來的內容，見 finishLiveSession 的 404 退路。
// 取後端每段回傳的完整 transcript 而不是自己串接：段落可能亂序辨識完，
// 後端是依錄音順序的 index 填槽，它的版本才是對的。
let liveTranscriptText = "";

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
    if (r.transcript) liveTranscriptText = r.transcript;
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
    liveStream = await buildLiveStream(wantSystemAudio());
  } catch (e) { releaseLiveStreams(); showError(e.message); return; }
  try {
    liveSessionId = (await jsonOrThrow(await fetch("/api/live/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ translate_to: $("liveTranslate").value || null }),
    }))).session_id;
  } catch (e) { releaseLiveStreams(); showError(e.message); return; }

  liveRecording = true;
  liveStartTime = Date.now();
  liveSegIndex = 0;
  liveSentCount = 0;
  liveSpeakers = {};
  liveTranscriptText = "";
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
  openStage();
});

$("btnLiveStop").addEventListener("click", async () => {
  clearError();
  liveRecording = false;
  clearTimeout(liveSegTimer);
  clearInterval(liveTickTimer);
  releaseWakeLock();
  closeStage();
  releaseStageAudio();
  $("btnLiveStage").style.display = "none";
  $("btnLiveStop").disabled = true;
  $("liveStatus").textContent = "整理最後一段錄音…";

  if (liveRecorder && liveRecorder.state !== "inactive") liveRecorder.stop();  // 觸發最後一段上傳
  releaseLiveStreams();  // 關掉麥克風、系統音源與混音器

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
  showResultSkeleton();
  const options = {
    meeting_date: $("meetingDate").value || null,
    kind: $("meetingKind").value,
    features: selectedFeatures(),
    correct_typos: correctTypos(),
    name_speakers: nameSpeakers(),
  };
  try {
    let result;
    try {
      result = await jsonOrThrow(await fetch(`/api/live/${liveSessionId}/finish`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(options),
      }));
    } catch (e) {
      // 404＝聆聽 session 不見了。session 只存在伺服器記憶體，行程一重啟（雲端
      // 重新部署、當掉重生）就永遠找不回來，再按幾次「重試分析」都是同樣的 404。
      // 但逐字稿在瀏覽器這邊還有一份，改走純文字分析把它救回來——這條路是「貼上
      // 文字」既有的流程，不需要 session。
      if (e.status !== 404 || !liveTranscriptText.trim()) throw e;
      result = await jsonOrThrow(await fetch("/api/meetings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: liveTranscriptText, ...options }),
      }));
      // 重啟之後送出的錄音段也會一起 404，所以這份逐字稿可能缺了後半段——
      // 寧可講清楚，也不要讓使用者以為分析的是完整的一場會議
      showNotice("聆聽 session 已遺失（伺服器可能重啟過），已改用瀏覽器保留的逐字稿分析。請核對逐字稿結尾是否完整。");
    }
    $("liveStatus").textContent = "完成";
    liveSessionId = null;
    $("btnLiveStart").disabled = false;
    renderResult(result, result.transcript);
  } catch (e) {
    // 404 走到這裡代表上面的退路也救不了：session 沒了、瀏覽器這份逐字稿又是空的。
    // 這種情況重試永遠是同一個 404，不該再擺一顆按不出結果的按鈕給使用者按
    const unrecoverable = e.status === 404;
    hideResultSkeleton();  // 退回「新會議」，重試分析的按鈕也在那裡
    showError("分析失敗：" + e.message + (unrecoverable
      ? "（這場聆聽沒有留下任何逐字稿，無法分析）"
      : "（逐字稿仍在，可按「重試分析」再試一次）"));
    $("liveStatus").textContent = "分析失敗";
    $("btnLiveStart").disabled = false;  // 也可放棄、重新開始新的一場
    if (!unrecoverable) {
      $("btnLiveRetry").style.display = "inline-flex";
      $("btnLiveRetry").disabled = false;
    }
  }
}
$("btnLiveRetry").addEventListener("click", finishLiveSession);

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
        <button class="del-btn" data-i="${i}" title="刪除此詞彙" aria-label="刪除">${icon("x", "i-sm")}</button>
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
    const r = await jsonOrThrow(await fetch("/api/speakers", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names: rosterNames }),
    }));
    rosterNames = r.names;
  } catch (err) {
    showError("儲存講者名冊失敗：" + err.message);
    rosterNames = (await jsonOrThrow(await fetch("/api/speakers"))).names;  // 退回伺服器版本
  }
  renderRoster();
}

// 記一個剛用到的姓名（手動改講者名時呼叫）。名冊記不記得起來都不影響改名本身，
// 所以失敗只當沒發生，不打擾使用者
async function rememberSpeaker(name) {
  try {
    await fetch("/api/speakers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names: [name] }),
    });
  } catch (err) { /* 名冊是加分項，靜靜略過 */ }
}

$("btnRoster").addEventListener("click", async () => {
  $("settingsMenu").classList.remove("open");
  $("rosterModal").classList.add("open");
  try {
    rosterNames = (await jsonOrThrow(await fetch("/api/speakers"))).names;
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
