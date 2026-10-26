import { $, clearError } from "./core.js";

/* ==================================================================
   0. 版面路由：側欄一次只顯示一個 view
   ------------------------------------------------------------------
   所有 view 的資料在載入時就一起抓（refreshTasks/Meetings/Reminders），
   切換只是顯示/隱藏，不重新請求，所以切分頁沒有等待感。
   ================================================================== */
// 結果頁的雙欄斷點，和 style.css 的 @media (max-width: 1180px) 對齊
const WIDE = window.matchMedia("(min-width: 1181px)");
const VIEW_TITLES = {
  home: "首頁", new: "新會議", result: "會議內容",
  reminder: "主動提醒", ask: "詢問會議", task: "任務庫", meeting: "歷史會議",
};
function showView(name) {
  if (!VIEW_TITLES[name]) name = "home";
  // 結果頁沒有側欄入口，只有「歷史會議 → 完整檢視」與剛分析完會開它。
  // 還沒開過任何一場時它是空的，別讓網址列直接跳進去
  if (name === "result" && $("result").style.display !== "flex") name = "home";
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("active", v.id === `view-${name}`));
  // 會議內容沒有自己的分頁鈕（它是歷史會議的完整檢視），高亮留在「歷史會議」，
  // 不然側欄會整排都沒亮，看不出自己在哪一區
  const tab = name === "result" ? "meeting" : name;
  document.querySelectorAll(".nav-item").forEach(b => {
    const on = b.dataset.view === tab;
    b.classList.toggle("active", on);
    b.setAttribute("aria-current", on ? "page" : "false");
  });
  $("viewTitle").textContent = VIEW_TITLES[name];
  if (location.hash.slice(1) !== name) history.replaceState(null, "", `#${name}`);
  window.scrollTo(0, 0);
}
// 側欄、數字磚、面板上的「看全部」共用同一個屬性，不必各自綁事件
// 使用者自己換頁（點側欄、看全部、瀏覽器上一頁）時清掉橫幅：上一頁的錯誤跟新的頁面無關。
// 程式自己切頁（分析完跳結果頁）不清——那時常常剛顯示一則跟結果有關的提示
document.addEventListener("click", e => {
  const el = e.target.closest("[data-view]");
  if (!el) return;
  clearError();
  showView(el.dataset.view);
});
window.addEventListener("hashchange", () => {
  clearError();
  showView(location.hash.slice(1));
});
showView(location.hash.slice(1) || "home");

export { VIEW_TITLES, WIDE, showView };
