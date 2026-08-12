import { $, esc, icon, jsonOrThrow } from "./core.js";
import { allMeetings, openMeetingDetail } from "./meetings.js";
import { showView } from "./router.js";

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

export { askScopeIds, hideSearchHits, renderAskScope, searchTimer, sendAsk, snippetHtml };
