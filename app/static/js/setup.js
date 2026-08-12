import { $, esc, jsonOrThrow } from "./core.js";

let chunkSeconds = 45;

/* ==================================================================
   2. 全域初始化：會議日期、錄音種類、功能勾選、分頁切換
   ================================================================== */
// ---- 初始化 ----
$("meetingDate").value = new Date().toLocaleDateString("sv");  // YYYY-MM-DD（本地時區）

// 會議種類：選單內容、每種類的預設區塊與說明文字全部來自 /api/meeting-kinds，
// 前端不自己抄一份名單（兩邊各維護一份必定會不同步）
const FEATURE_BOX = { summary: "featSummary", highlights: "featHighlights", decisions: "featDecisions", todos: "featTodos" };
let kindDefaults = {};   // 種類 → 預設開啟的區塊
let kindHints = {};      // 種類 → 這個種類的分析重點說明
// 使用者自己動過勾選框就別再覆蓋他，除非他換了種類
let featuresTouched = false;

function applyKindDefaults() {
  const kind = $("meetingKind").value;
  $("kindHint").textContent = kindHints[kind] || "";
  const on = kindDefaults[kind];
  if (!on) return;
  for (const [key, id] of Object.entries(FEATURE_BOX)) $(id).checked = on.includes(key);
}

(async function initMeetingKinds() {
  const sel = $("meetingKind");
  try {
    const r = await jsonOrThrow(await fetch("/api/meeting-kinds"));
    sel.innerHTML = r.groups.map(g =>
      `<optgroup label="${esc(g.label)}">${g.kinds.map(k =>
        `<option value="${esc(k.value)}">${esc(k.value)}</option>`).join("")}</optgroup>`).join("");
    r.groups.forEach(g => g.kinds.forEach(k => {
      kindDefaults[k.value] = k.features;
      kindHints[k.value] = k.hint;
    }));
    const saved = localStorage.getItem("meetingKind");
    sel.value = saved && kindDefaults[saved] ? saved : r.default;
  } catch (e) {
    // 選單載不到就維持 HTML 裡那個「一般會議」，不擋住整個分析流程
  }
  applyKindDefaults();
  sel.addEventListener("change", () => {
    localStorage.setItem("meetingKind", sel.value);
    featuresTouched = false;  // 換種類＝重新套用該種類的預設
    applyKindDefaults();
  });
  Object.values(FEATURE_BOX).forEach(id =>
    $(id).addEventListener("change", () => { featuresTouched = true; }));
})();

// 目前的功能勾選狀態。使用者沒動過就回 null，讓後端套用該種類的預設——
// 這樣預設值只定義在後端一處，前端不會因為勾選框的初始狀態而蓋掉它
function selectedFeatures() {
  if (!featuresTouched) return null;
  return Object.entries(FEATURE_BOX).filter(([, id]) => $(id).checked).map(([key]) => key);
}

// 本次專用詞彙：使用者用頓號/逗號分隔隨手打，這裡轉成後端要的格式
function meetingTerms() {
  return $("meetingTerms").value
    .split(/[、,，;；\n]/)
    .map(s => s.trim())
    .filter(Boolean)
    .map(term => ({ term, note: "" }));
}

// 勾了「一併加入全域詞彙表」就把本次的詞補進去。失敗只是少了個方便功能，
// 不該讓已經跑完的分析看起來像出錯，所以靜靜略過
async function maybePromoteTerms() {
  const terms = meetingTerms();
  if (!$("termsToGlossary").checked || !terms.length) return;
  try {
    const existing = (await jsonOrThrow(await fetch("/api/glossary"))).terms;
    const known = new Set(existing.map(t => t.term));
    const merged = existing.concat(terms.filter(t => !known.has(t.term)));
    if (merged.length === existing.length) return;
    await fetch("/api/glossary", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ terms: merged }),
    });
  } catch (e) { /* 加不進去不影響這次分析 */ }
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

export { FEATURE_BOX, LOOPBACK_RE, applyKindDefaults, chunkSeconds, correctTypos, featuresTouched, kindDefaults, kindHints, loadHealth, maybePromoteTerms, meetingTerms, nameSpeakers, populateSysSources, selectedFeatures, sysSourceValue, wantSystemAudio };
