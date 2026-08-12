const $ = id => document.getElementById(id);
const PRIORITY_ZH = { high: "高", medium: "中", low: "低" };

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

// 各清單自己登記重繪函式：core 不必反過來 import 三個清單模組（會形成循環）
const PAGERS = {};
function registerPager(kind, render) { PAGERS[kind] = render; }

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
  PAGERS[kind]?.();
  // 翻頁後要從新一頁的開頭看起，不然會停在上一頁的位置
  $(`${kind}Pager`).closest(".panel").scrollIntoView({ behavior: "smooth", block: "start" });
});

// 「重試」按鈕要重抓哪一份資料，由各清單模組自己登記。
// core 若反過來 import 它們會形成求值期循環：core 是第一個被載入的模組，
// 屆時它匯出的 $ / esc 都還在 TDZ，清單模組頂層註冊事件就會炸掉。
const REFRESHERS = {};
function registerRefresher(kind, fn) { REFRESHERS[kind] = fn; }

function loadFail(kind) {
  return `<p class="load-fail">${icon("circle-alert", "i-sm")}載不到資料<button class="ghost" data-retry="${kind}">重試</button></p>`;
}
document.addEventListener("click", e => {
  const btn = e.target.closest("[data-retry]");
  if (!btn) return;
  REFRESHERS[btn.dataset.retry]?.();
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

export { $, API_TOKEN_KEY, PAGERS, PAGE_SIZE, PRIORITY_ZH, REFRESHERS, clearError, esc, icon, jsonOrThrow, loadFail, nativeFetch, pageNo, paginate, registerPager, registerRefresher, renderPager, showError, showNotice, skelBlocks, skelLine, skelRows };
