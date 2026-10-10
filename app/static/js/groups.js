import { api } from "./api.js";
import { $, currentWorkspace, esc, icon, nativeFetch, showError, showNotice, switchWorkspace } from "./core.js";
import { inviteMailUrl, roleLabel } from "./groupinvite.js";
import { signedInUser } from "./auth.js";
import { inputBusy } from "./inputs.js";

/* ==================================================================
   11. 工作區：個人／群組切換、群組管理與邀請
   ------------------------------------------------------------------
   只在開了 Google 登入時存在（沒登入就沒有「誰」可以邀請）：/api/groups 回 404
   代表沒開，整個入口保持隱藏。切到群組後全站顯示群組的資料，靠的是 core.js
   幫每個請求帶 X-Workspace，各頁面的程式完全不用知道群組存在。
   ================================================================== */
let state = { me: null, groups: [], invites: [] };

const currentGroup = () => state.groups.find(g => g.id === currentWorkspace()) || null;

async function loadGroups() {
  // 先看有沒有開登入：沒開就不打 /api/groups，免得每次開頁都在 console 留一筆 404
  try {
    const cfg = await (await nativeFetch("/api/auth/config")).json();
    if (!cfg.enabled) return;
    const me = await signedInUser();
    if (!me || me.guest) return;  // 訪客不能用群組，入口不顯示
    state = await api.groups();
  } catch (err) {
    console.warn("讀取群組失敗：", err.message);
    return;  // 先不顯示切換，不影響個人工作區
  }
  const ws = currentWorkspace();
  if (ws && !currentGroup()) {
    switchWorkspace("", "你已不在原本的群組（可能已被移出或群組已解散），已切回個人工作區。");
    return;
  }
  $("wsSwitch").hidden = false;
  $("wsChip").hidden = false;
  renderSwitch();
}

function renderSwitch() {
  const g = currentGroup();
  const label = g ? g.name : "個人";
  $("wsName").textContent = label;
  $("wsChip").innerHTML = `${icon("users", "i-sm")}${esc(label)}${g ? ` · ${esc(roleLabel(g.role))}` : ""}`;
  const n = state.invites.length;
  $("wsInviteBadge").hidden = !n;
  $("wsInviteBadge").textContent = n ? String(n) : "";
}

function menuHtml() {
  const ws = currentWorkspace();
  const check = on => (on ? icon("check", "i-sm") : `<span class="ws-check-gap"></span>`);
  const groups = state.groups.map(g => `
      <button class="ws-item" data-ws="${esc(g.id)}">${check(g.id === ws)}
        <span class="ws-item-name">${esc(g.name)}</span><span class="ws-role">${esc(roleLabel(g.role))}</span>
      </button>`).join("");
  const invites = state.invites.length
    ? `<div class="ws-sep"></div><div class="ws-head">待接受的邀請</div>` + state.invites.map(i => `
        <div class="ws-invite">
          <div><b>${esc(i.name)}</b><span class="ws-role">${esc(roleLabel(i.role))}</span>
            ${i.invited_by ? `<div class="ws-by">${esc(i.invited_by)} 邀請你</div>` : ""}</div>
          <div class="ws-invite-ops">
            <button class="primary" data-accept="${esc(i.id)}">接受</button>
            <button class="ghost" data-decline="${esc(i.id)}">拒絕</button>
          </div>
        </div>`).join("")
    : "";
  const g = currentGroup();
  return `
    <div class="ws-head">工作區</div>
    <button class="ws-item" data-ws="">${check(!ws)}<span class="ws-item-name">個人</span></button>
    ${groups}
    ${invites}
    <div class="ws-sep"></div>
    <button class="ws-item" data-create>${icon("plus", "i-sm")}<span class="ws-item-name">建立群組</span></button>
    ${g ? `<button class="ws-item" data-manage>${icon("settings", "i-sm")}<span class="ws-item-name">管理「${esc(g.name)}」</span></button>` : ""}`;
}

function openMenu(anchor) {
  const menu = $("wsMenu");
  menu.innerHTML = menuHtml();
  menu.hidden = false;
  const r = anchor.getBoundingClientRect();
  // 手機的按鈕在頂列右側：選單靠右對齊、不要超出畫面
  const left = Math.min(r.left, window.innerWidth - menu.offsetWidth - 12);
  menu.style.left = `${Math.max(12, left)}px`;
  menu.style.top = `${r.bottom + 6}px`;
  anchor.setAttribute("aria-expanded", "true");
}
function closeMenu() {
  $("wsMenu").hidden = true;
  $("wsBtn").setAttribute("aria-expanded", "false");
}

for (const id of ["wsBtn", "wsChip"]) {
  $(id).addEventListener("click", e => {
    e.stopPropagation();
    if ($("wsMenu").hidden) openMenu($(id)); else closeMenu();
  });
}
document.addEventListener("click", e => {
  if (!$("wsMenu").hidden && !e.target.closest("#wsMenu")) closeMenu();
});

// 聆聽、上傳轉錄、分析進行中不給切：後續請求會帶新的工作區，進行中的工作就找不到了
function goTo(id, notice) {
  if (inputBusy()) {
    showError("正在即時聆聽、上傳轉錄或分析中，完成後再切換工作區。");
    return;
  }
  switchWorkspace(id, notice);
}

$("wsMenu").addEventListener("click", async e => {
  const item = e.target.closest("[data-ws]");
  if (item) {
    closeMenu();
    if (item.dataset.ws !== currentWorkspace()) goTo(item.dataset.ws);
    return;
  }
  if (e.target.closest("[data-create]")) {
    closeMenu();
    const name = (prompt("群組名稱（例如：專題小組）") || "").trim();
    if (!name) return;
    try {
      const r = await api.createGroup(name);
      state = r;
      goTo(r.id, `已建立群組「${name}」。從工作區選單的「管理」邀請成員。`);
    } catch (err) { showError("建立群組失敗：" + err.message); }
    return;
  }
  if (e.target.closest("[data-manage]")) { closeMenu(); openManage(); return; }
  const accept = e.target.closest("[data-accept]");
  if (accept) {
    const inv = state.invites.find(i => i.id === accept.dataset.accept);
    try {
      state = await api.acceptInvite(accept.dataset.accept);
      goTo(accept.dataset.accept, `已加入群組「${inv ? inv.name : ""}」。`);
    } catch (err) { showError("接受邀請失敗：" + err.message); }
    return;
  }
  const decline = e.target.closest("[data-decline]");
  if (decline) {
    try {
      state = await api.declineInvite(decline.dataset.decline);
      renderSwitch();
      $("wsMenu").innerHTML = menuHtml();
    } catch (err) { showError("拒絕邀請失敗：" + err.message); }
  }
});

// ---- 群組管理視窗 ----
const roleSelect = (cls, value, extra = "") =>
  `<select class="field-sel ${cls}" ${extra}>${["editor", "viewer"].map(r =>
    `<option value="${r}" ${r === value ? "selected" : ""}>${roleLabel(r)}</option>`).join("")}</select>`;

function manageHtml(g) {
  const owner = g.role === "owner";
  const meUid = state.me && state.me.uid;
  const members = g.members.map(m => `
      <div class="grp-row">
        <span class="grp-who">${esc(m.email || m.uid)}${m.uid === meUid ? "（你）" : ""}</span>
        ${owner && m.role !== "owner"
          ? `${roleSelect("grp-role", m.role, `data-uid="${esc(m.uid)}"`)}
             <button class="ghost" data-remove="${esc(m.uid)}">移除</button>`
          : `<span class="ws-role">${esc(roleLabel(m.role))}</span>`}
      </div>`).join("");
  const invites = g.invites.length ? g.invites.map(i => `
      <div class="grp-row">
        <span class="grp-who">${esc(i.email)}</span>
        <span class="ws-role">${esc(roleLabel(i.role))}・待接受</span>
        ${owner ? `<a class="ghost btn-link" target="_blank" rel="noopener"
            href="${esc(inviteMailUrl({ to: i.email, groupName: g.name, role: i.role,
                                        inviter: state.me && state.me.email, siteUrl: location.origin }))}">用 Gmail 寄邀請信</a>
          <button class="ghost" data-revoke="${esc(i.email)}">撤回</button>` : ""}
      </div>`).join("") : `<p class="empty-note">沒有待接受的邀請</p>`;
  return `
    ${owner ? `<div class="grp-rename"><input type="text" id="grpName" value="${esc(g.name)}" maxlength="40">
      <button class="ghost" id="btnGrpRename">改名</button></div>` : ""}
    <h4>成員</h4>${members}
    <h4>邀請</h4>
    ${owner ? `<div class="grp-invite">
        <input type="email" id="grpInviteEmail" placeholder="對方的 Google 信箱">
        ${roleSelect("", "editor", 'id="grpInviteRole"')}
        <button class="primary" id="btnGrpInvite">邀請</button>
      </div>
      <p class="hint">對方用這個信箱登入本站，就會在工作區選單看到邀請；也可以按「用 Gmail 寄邀請信」通知他（信由你自己寄出）。</p>` : ""}
    ${invites}
    <div class="grp-danger">
      ${owner
        ? `<button class="ghost danger" id="btnGrpDisband">解散群組（刪除群組內所有會議與任務）</button>`
        : `<button class="ghost danger" id="btnGrpLeave">退出群組</button>`}
    </div>`;
}

function openManage() {
  const g = currentGroup();
  if (!g) return;
  $("groupModalTitle").textContent = `管理「${g.name}」`;
  $("groupModalBody").innerHTML = manageHtml(g);
  $("groupModal").classList.add("open");
}
function refreshManage(next) {
  state = next;
  renderSwitch();
  if (currentGroup()) openManage();
}

$("btnGroupClose").addEventListener("click", () => $("groupModal").classList.remove("open"));
$("groupModal").addEventListener("click", e => {
  if (e.target === $("groupModal")) $("groupModal").classList.remove("open");
});

$("groupModalBody").addEventListener("click", async e => {
  const g = currentGroup();
  if (!g) return;
  try {
    if (e.target.closest("#btnGrpInvite")) {
      const email = $("grpInviteEmail").value.trim();
      if (!email) return;
      refreshManage(await api.inviteToGroup(g.id, email, $("grpInviteRole").value));
      showNotice(`已邀請 ${email}。對方用這個信箱登入就會看到；也可以按「用 Gmail 寄邀請信」通知他。`);
    } else if (e.target.closest("#btnGrpRename")) {
      refreshManage(await api.renameGroup(g.id, $("grpName").value));
    } else if (e.target.closest("[data-revoke]")) {
      refreshManage(await api.revokeInvite(g.id, e.target.closest("[data-revoke]").dataset.revoke));
    } else if (e.target.closest("[data-remove]")) {
      const uid = e.target.closest("[data-remove]").dataset.remove;
      const who = g.members.find(m => m.uid === uid);
      if (!confirm(`要把 ${who ? who.email : uid} 移出群組嗎？`)) return;
      refreshManage(await api.removeMember(g.id, uid));
    } else if (e.target.closest("#btnGrpLeave")) {
      if (!confirm(`要退出「${g.name}」嗎？退出後就看不到群組的會議與任務。`)) return;
      state = await api.removeMember(g.id, state.me.uid);
      $("groupModal").classList.remove("open");
      switchWorkspace("", `已退出群組「${g.name}」。`);
    } else if (e.target.closest("#btnGrpDisband")) {
      // 刪掉的是所有成員的資料，要多打一次群組名稱，不是按一下確定就好
      const typed = prompt(`解散後群組裡的所有會議、任務、詞彙都會刪除，無法復原。\n請輸入群組名稱「${g.name}」確認：`);
      if (typed === null) return;
      if (typed.trim() !== g.name) { showError("群組名稱不符，沒有解散。"); return; }
      state = await api.disbandGroup(g.id);
      $("groupModal").classList.remove("open");
      switchWorkspace("", `已解散群組「${g.name}」。`);
    }
  } catch (err) { showError("群組操作失敗：" + err.message); }
});

$("groupModalBody").addEventListener("change", async e => {
  const sel = e.target.closest(".grp-role");
  const g = currentGroup();
  if (!sel || !g) return;
  try { refreshManage(await api.setMemberRole(g.id, sel.dataset.uid, sel.value)); }
  catch (err) { showError("更改角色失敗：" + err.message); }
});

window.addEventListener("workspacechange", renderSwitch);
// 換了帳號：上一個人的群組不能留在選單裡
window.addEventListener("accountchange", () => {
  state = { me: null, groups: [], invites: [] };
  $("wsSwitch").hidden = true;
  $("wsChip").hidden = true;
  closeMenu();
  loadGroups();
});

loadGroups();

export { currentGroup, loadGroups };
