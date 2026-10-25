/* ==================================================================
   逐字稿就地修正的介面：點一句 → 改文字／改講者／合併／拆開／刪除
   ------------------------------------------------------------------
   轉錄聽錯一兩個字是最常見的抱怨，但原本只能做「整個詞統一替換」與「講者改名」，
   單句沒辦法改。這裡把 transcriptedit.js 的純函式接到畫面上。

   編輯哪一句靠泡泡的 data-i（＝ editableBubbles 的序號，兩邊同一套切分）。
   存檔走既有的 PATCH /api/meetings/{id}，後端會順手作廢問答索引。

   改過之後分析結果（摘要、代辦、引用句）仍是舊的，所以底下會出現「重新分析」。
   刻意不自動重跑：重新分析要花 AI 額度，改一個錯字不值得。
   ================================================================== */
import { api } from "./api.js";
import { $, esc, showError } from "./core.js";
import { renderChat } from "./transcript.js";
import { deleteBubble, editBubble, editableBubbles, mergeWithNext, splitBubble } from "./transcriptedit.js";

// 目前開著編輯框的泡泡序號；null＝沒有
let openIndex = null;

// 由 result.js 提供：讀寫目前這場會議的逐字稿與 id（結果頁與歷史會議完整檢視共用）
let ctx = null;
function bindTranscriptEditor(context) {
  ctx = context;
  closeEditor();
  const editable = Boolean(ctx?.meetingId());
  $("btnEditTranscript").hidden = !editable;
  setEditing(false);
  $("transEditFoot").hidden = true;
}

function setEditing(on) {
  $("rTranscript").classList.toggle("editing", on);
  $("transEditHint").hidden = !on;
  $("btnEditTranscript").classList.toggle("active", on);
  if (!on) closeEditor();
}
function isEditing() {
  return $("rTranscript").classList.contains("editing");
}

// 收掉編輯框：整份重畫，不能只把那一行清空——清空會留下一條空白的句子
function closeEditor() {
  if (openIndex === null) return;
  openIndex = null;
  if (ctx) renderChat($("rTranscript"), ctx.transcript(), ctx.knownNames());
}

function speakersOf(text) {
  return [...new Set(editableBubbles(text, ctx?.knownNames() || []).map(b => b.speaker).filter(Boolean))];
}

// 把一個泡泡換成編輯框。保留原本的時間欄，編輯中的那一行不再是純文字
function openEditor(line, index) {
  if (!ctx) return;
  const text = ctx.transcript();
  const bubbles = editableBubbles(text, ctx.knownNames());
  const bubble = bubbles[index];
  if (!bubble) return;
  const hasNext = index < bubbles.length - 1;
  openIndex = index;
  const options = speakersOf(text)
    .map(s => `<option value="${esc(s)}"${s === bubble.speaker ? " selected" : ""}>${esc(s)}</option>`).join("");
  line.innerHTML =
    `<span class="line-time">${bubble.time ? esc(bubble.time) : ""}</span>` +
    `<span class="line-body"><div class="line-edit">` +
    `<textarea class="edit-text" aria-label="這一句的內容">${esc(bubble.text)}</textarea>` +
    `<div class="line-edit-bar">` +
      `<select class="edit-speaker" aria-label="講者"><option value="">（無講者）</option>${options}` +
        `<option value="__new">其他…</option></select>` +
      (hasNext ? `<button class="ghost" data-act="merge" title="與下一句合併">合併下一句</button>` : "") +
      `<button class="ghost" data-act="split" title="從游標位置拆成兩句">從游標拆開</button>` +
      `<button class="ghost danger" data-act="delete">刪除</button>` +
      `<span class="spacer"></span>` +
      `<button class="ghost" data-act="cancel">取消</button>` +
      `<button class="primary" data-act="save">儲存</button>` +
    `</div></div></span>`;
  const area = line.querySelector(".edit-text");
  area.focus();
  area.setSelectionRange(area.value.length, area.value.length);
}

// 存回後端並重畫。next＝新的整份逐字稿
async function commit(next) {
  const id = ctx?.meetingId();
  if (!id || next === null || next === undefined) return;
  try {
    await api.updateMeeting(id, { transcript: next });
  } catch (err) {
    showError("儲存逐字稿失敗：" + err.message);
    return;
  }
  ctx.setTranscript(next);
  openIndex = null;
  renderChat($("rTranscript"), next, ctx.knownNames());
  $("rTranscript").classList.add("editing");
  $("transEditFoot").hidden = false;
}

function wireTranscriptEditor() {
  $("btnEditTranscript").addEventListener("click", e => {
    e.preventDefault();   // 按鈕在 <summary> 裡，避免同時觸發展開／收合
    e.stopPropagation();
    setEditing(!isEditing());
  });

  // 按下按鈕時不要讓輸入框失焦：「從游標拆開」要讀 selectionStart，失焦後會歸零
  $("rTranscript").addEventListener("mousedown", e => {
    if (e.target.closest("button[data-act]")) e.preventDefault();
  });

  $("rTranscript").addEventListener("click", e => {
    if (!isEditing() || !ctx) return;
    const act = e.target.closest("[data-act]")?.dataset.act;
    const line = e.target.closest(".chat-line");
    if (!act) {
      // 點另一句＝換過去編輯；點已經開著的那句不動（才選得到文字）
      if (!line || line.querySelector(".line-edit")) return;
      const index = Number(line.dataset.i);
      if (openIndex !== null) renderChat($("rTranscript"), ctx.transcript(), ctx.knownNames());
      openEditor($("rTranscript").querySelector(`.chat-line[data-i="${index}"]`) || line, index);
      return;
    }
    const index = openIndex;
    const text = ctx.transcript();
    if (act === "cancel") {
      renderChat($("rTranscript"), text, ctx.knownNames());
      openIndex = null;
      return;
    }
    if (act === "save") {
      const body = line.querySelector(".edit-text").value;
      const picked = line.querySelector(".edit-speaker").value;
      const speaker = picked === "__new" ? (prompt("這一句的講者是誰？") || "").trim() : picked;
      if (picked === "__new" && !speaker) return;
      const next = editBubble(text, index, { text: body, speaker });
      if (next === null) { showError("內容不能是空白；要刪掉整句請按「刪除」。"); return; }
      commit(next);
      return;
    }
    if (act === "merge") { commit(mergeWithNext(text, index, ctx.knownNames())); return; }
    if (act === "split") {
      const at = line.querySelector(".edit-text").selectionStart;
      const next = splitBubble(text, index, at, ctx.knownNames());
      if (next === null) { showError("游標在句子的開頭或結尾，沒有地方可以拆。"); return; }
      commit(next);
      return;
    }
    if (act === "delete") {
      if (!confirm("刪掉這一句？")) return;
      commit(deleteBubble(text, index, ctx.knownNames()));
    }
  });

  $("btnReanalyzeEdited").addEventListener("click", () => ctx?.reanalyze());
}

export { bindTranscriptEditor, wireTranscriptEditor };
