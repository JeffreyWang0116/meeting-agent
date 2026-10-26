/* ==================================================================
   會議前準備：議程、會議系列、把上次沒做完的帶過來
   ------------------------------------------------------------------
   散會後最常見的兩個問題是「議程上有一項好像沒講到」和「上次講好的那件事
   後來呢」。兩個都不該靠重讀逐字稿回答——議程是會前就寫好的，上次沒做完的
   事資料庫裡也有。
   ================================================================== */
import { api } from "./api.js";
import { $, esc } from "./core.js";

let carry = { from: null, lines: [] };

// 三條輸入路徑（貼文字／上傳／即時聆聽）共用同一份會前設定
function prepFields() {
  const agenda = $("prepAgenda").value.trim();
  const series = $("prepSeries").value.trim();
  return {
    agenda: agenda || null,
    series: series || null,
    // 沒有系列可參照時，勾了也沒東西可帶
    carry_over: !!(series && $("prepCarry").checked && carry.lines.length),
  };
}

async function loadSeries() {
  try {
    const names = (await api.listSeries()).series || [];
    $("prepSeriesList").innerHTML = names.map(n => `<option value="${esc(n)}">`).join("");
  } catch (e) { /* 下拉提示而已，拿不到就讓使用者自己打 */ }
}

function renderCarry() {
  const row = $("prepCarryRow");
  const list = $("prepCarryList");
  const has = carry.lines.length > 0;
  row.hidden = !has;
  list.hidden = !has;
  if (!has) { list.innerHTML = ""; return; }
  const when = carry.from ? `${carry.from.date || ""} ${carry.from.title || ""}`.trim() : "上次";
  list.innerHTML = `<p class="prep-from">上次（${esc(when)}）還沒做完：</p>`
    + `<ul>${carry.lines.map(l => `<li>${esc(l)}</li>`).join("")}</ul>`;
}

// 打字打到一半就一直打 API 沒有意義，停下來才查
let timer = null;
async function lookupCarry() {
  const series = $("prepSeries").value.trim();
  if (!series) { carry = { from: null, lines: [] }; renderCarry(); return; }
  try {
    carry = await api.seriesCarryover(series);
  } catch (e) {
    carry = { from: null, lines: [] };
  }
  renderCarry();
}

$("prepSeries").addEventListener("input", () => {
  clearTimeout(timer);
  timer = setTimeout(lookupCarry, 400);
});

// 切換工作區：系列與上次未完成都是那個工作區的事
window.addEventListener("workspacechange", () => {
  carry = { from: null, lines: [] };
  renderCarry();
  loadSeries();
});

loadSeries();

export { carry, loadSeries, prepFields };
