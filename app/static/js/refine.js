/* ==================================================================
   請 AI 修改分析結果：一句話 → 預覽 → 套用
   ------------------------------------------------------------------
   兩段式是刻意的。AI 默默改掉摘要或負責人，使用者不會發現——這和當初整條
   移除「讓模型猜講者姓名」是同一個理由。所以送出只拿到「會改成這樣」，
   按下套用才真的寫進去。

   套用那一步不打 AI（後端直接套前端送回的 patch），所以預覽完一定套得上，
   不會卡在額度用完。
   ================================================================== */
import { api } from "./api.js";
import { $, esc, showError, showNotice } from "./core.js";

let ctx = null;        // 由 result.js 提供：目前這場會議的 id 與套用後的重繪
let pendingPatch = null;

function bindRefine(context) {
  ctx = context;
  pendingPatch = null;
  $("rRefine").hidden = !context?.meetingId();
  $("refineInput").value = "";
  hidePreview();
}

function hidePreview() {
  pendingPatch = null;
  $("refinePreview").hidden = true;
  $("refineChanges").innerHTML = "";
}

// before/after 都可能是 null（原本沒填）或長字串，顯示時要看得出「從什麼變成什麼」
function changeHtml(c) {
  const before = c.before === null || c.before === undefined || c.before === ""
    ? "（空）" : String(c.before);
  const after = c.after === null || c.after === undefined || c.after === ""
    ? "（清空）" : String(c.after);
  return `<div class="refine-change"><b>${esc(c.what)}</b><br>` +
    `<span class="before">${esc(before)}</span> → <span class="after">${esc(after)}</span></div>`;
}

async function askRefine() {
  const request = $("refineInput").value.trim();
  if (!request || !ctx?.meetingId()) return;
  const btn = $("btnRefine");
  btn.disabled = true;
  try {
    const { changes, patch } = await api.refine(ctx.meetingId(), { request });
    if (!changes.length) {
      showNotice("AI 沒有找到需要修改的地方。換個說法試試，例如「把摘要改成兩句話」。");
      hidePreview();
      return;
    }
    pendingPatch = patch;
    $("refineChanges").innerHTML = changes.map(changeHtml).join("");
    $("refinePreview").hidden = false;
  } catch (err) { showError("請 AI 修改失敗：" + err.message); }
  finally { btn.disabled = false; }
}

async function applyRefine() {
  if (!pendingPatch || !ctx?.meetingId()) return;
  const btn = $("btnRefineApply");
  btn.disabled = true;
  try {
    const { meeting, tasks, changes } = await api.refine(ctx.meetingId(), { patch: pendingPatch });
    hidePreview();
    $("refineInput").value = "";
    showNotice(`已套用 ${changes.length} 項修改。`);
    ctx.applied(meeting, tasks);
  } catch (err) { showError("套用失敗：" + err.message); }
  finally { btn.disabled = false; }
}

function wireRefine() {
  $("btnRefine").addEventListener("click", askRefine);
  $("refineInput").addEventListener("keydown", e => {
    if (e.key === "Enter") askRefine();
  });
  $("btnRefineCancel").addEventListener("click", hidePreview);
  $("btnRefineApply").addEventListener("click", applyRefine);
}

export { bindRefine, wireRefine };
