/* ==================================================================
   留言：對摘要、決議、代辦、待確認事項討論
   ------------------------------------------------------------------
   AI 的產出常常只是「大致上對」。以前要反映「這條決議其實沒有定案」只有
   兩條路：直接改掉（別人不知道原本是什麼、也不知道為什麼），或者在群組
   聊天室講（那句話與這場會議從此失聯）。
   ================================================================== */
import { api } from "./api.js";
import { $, esc, showError } from "./core.js";

let meetingId = null;          // 目前看的是哪一場；null＝還沒存檔，不能留言
let counts = {};               // target -> 幾則，畫在按鈕上
let openTarget = null;         // modal 現在開著哪一個位置
let openLabel = "";

// 給 result.js 插在每一項旁邊。沒有 meetingId 時回空字串——剛貼上還沒存的
// 會議沒有地方可以掛留言，畫一顆按不動的按鈕只會讓人以為壞了
function commentBtn(target, quote = "") {
  if (!meetingId) return "";
  const n = counts[target] || 0;
  return `<button class="cmt-btn${n ? " has" : ""}" data-target="${esc(target)}"`
    + ` data-quote="${esc(quote)}" title="留言討論" aria-label="留言">`
    + `💬${n ? `<span class="cmt-n">${n}</span>` : ""}</button>`;
}

// 畫面已經畫好之後才拿到數字，不必整頁重繪，就地把徽章補上去
function paintCounts() {
  document.querySelectorAll("#result .cmt-btn").forEach(btn => {
    const n = counts[btn.dataset.target] || 0;
    btn.classList.toggle("has", n > 0);
    const badge = btn.querySelector(".cmt-n");
    if (n && badge) badge.textContent = n;
    else if (n) btn.insertAdjacentHTML("beforeend", `<span class="cmt-n">${n}</span>`);
    else if (badge) badge.remove();
  });
}

async function loadComments(id) {
  meetingId = id || null;
  counts = {};
  if (!meetingId) return;
  try {
    counts = (await api.listComments(meetingId)).counts || {};
  } catch (e) {
    counts = {};  // 留言讀不到不該讓整份結果看起來壞掉
  }
  paintCounts();
}

function threadHtml(comments) {
  if (!comments.length) return `<p class="empty-note">還沒有人留言</p>`;
  return comments.map(c => `<div class="cmt" data-id="${esc(c.id)}">
      <div class="cmt-head">
        <span class="cmt-who">${esc(c.author_name)}</span>
        <span class="cmt-when">${esc((c.at || "").slice(0, 16).replace("T", " "))}</span>
        ${c.mine ? `<button class="del-btn cmt-del" aria-label="刪除">×</button>` : ""}
      </div>
      <div class="cmt-text">${esc(c.text)}</div>
    </div>`).join("");
}

const WHERE = {
  summary: "會議摘要", transcript: "逐字稿",
  decision: "決議事項", pending: "待確認事項", highlight: "會議重點",
  task: "代辦事項", section: "重點欄位",
};

function whereLabel(target) {
  const kind = target.split(":")[0];
  return WHERE[kind] || "這一項";
}

// 任務庫與看板也要開得了留言，但那裡的 meetingId 不是「目前看的那一場」，
// 是任務自己所屬的那一場——所以開啟時一併帶進來
async function openFor(id, target, quote) {
  if (!id) {
    showError("手動新增的任務不屬於任何會議，沒有地方可以掛留言");
    return;
  }
  meetingId = id;
  await openThread(target, quote);
}

async function openThread(target, quote) {
  openTarget = target;
  openLabel = quote;
  $("cmtWhere").textContent = `留言 · ${whereLabel(target)}`;
  $("cmtQuote").textContent = quote || "";
  $("cmtQuote").hidden = !quote;
  $("cmtThread").innerHTML = `<p class="empty-note">載入中…</p>`;
  $("commentModal").classList.add("open");
  $("cmtInput").value = "";
  await renderThread();
  $("cmtInput").focus();
}

async function renderThread() {
  let body;
  try {
    body = await api.listComments(meetingId);
  } catch (e) {
    $("cmtThread").innerHTML = `<p class="empty-note">讀取失敗</p>`;
    return;
  }
  counts = body.counts || {};
  paintCounts();
  $("cmtThread").innerHTML = threadHtml((body.comments || []).filter(c => c.target === openTarget));
}

function closeThread() {
  $("commentModal").classList.remove("open");
  openTarget = null;
}

$("result").addEventListener("click", e => {
  const btn = e.target.closest(".cmt-btn");
  if (btn) openThread(btn.dataset.target, btn.dataset.quote);
});

$("btnCmtClose").addEventListener("click", closeThread);
$("commentModal").addEventListener("click", e => {
  if (e.target.id === "commentModal") closeThread();  // 點背景關掉
});

$("btnCmtSend").addEventListener("click", async () => {
  const text = $("cmtInput").value.trim();
  if (!text || !openTarget) return;
  const btn = $("btnCmtSend");
  btn.disabled = true;
  try {
    await api.addComment(meetingId, { target: openTarget, text });
    $("cmtInput").value = "";
    await renderThread();
  } catch (err) {
    showError(`留言失敗：${err.message}`);
  } finally {
    btn.disabled = false;
  }
});

// Ctrl/⌘+Enter 送出：留言常常只有一行，但換行也要留得住，所以不佔用 Enter
$("cmtInput").addEventListener("keydown", e => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("btnCmtSend").click();
});

$("cmtThread").addEventListener("click", async e => {
  const del = e.target.closest(".cmt-del");
  if (!del) return;
  try {
    await api.deleteComment(del.closest(".cmt").dataset.id);
    await renderThread();
  } catch (err) {
    showError(`刪除失敗：${err.message}`);
  }
});

// 任務庫／看板的留言鈕：meetingId 要跟著任務走
function taskCommentBtn(task) {
  if (!task.meeting_id) return "";
  const n = task.comment_count || 0;
  return `<button class="cmt-btn${n ? " has" : ""}" data-meeting="${esc(task.meeting_id)}"`
    + ` data-target="task:${esc(task.id)}" data-quote="${esc(task.task || "")}"`
    + ` title="留言討論" aria-label="留言">💬</button>`;
}

for (const id of ["taskGroups", "kanban"]) {
  $(id)?.addEventListener("click", e => {
    const btn = e.target.closest(".cmt-btn");
    if (btn) openFor(btn.dataset.meeting, btn.dataset.target, btn.dataset.quote);
  });
}

export { commentBtn, loadComments, openFor, taskCommentBtn, whereLabel };
