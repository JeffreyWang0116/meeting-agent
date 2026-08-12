import { $ } from "./core.js";

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

export { VIEW_TITLES, WIDE, showView };
