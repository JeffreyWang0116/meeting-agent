/* ==================================================================
   群組邀請：角色名稱、邀請信的 Gmail 草稿網址
   ------------------------------------------------------------------
   系統不申請 Gmail 寄信權限（敏感範圍，測試階段只有白名單帳號能用、正式上線
   要審核），改成開好收件人、主旨、內文的撰寫視窗，由邀請人自己按寄出——跟會議
   確認信同一個做法。

   純函式、不碰 DOM：tests/test_group_invite_js.py 直接用 node 載入測試。
   ================================================================== */
const ROLE_ZH = { owner: "建立者", editor: "可編輯", viewer: "只能看" };

function roleLabel(role) { return ROLE_ZH[role] || role; }

function inviteMailUrl({ to, groupName, role, inviter, siteUrl }) {
  const body = [
    "你好，",
    "",
    `${inviter || "我"} 邀請你加入會議助手的群組「${groupName}」（角色：${roleLabel(role)}）。`,
    "加入後可以一起看群組裡的會議紀錄、逐字稿與任務。",
    "",
    `1. 打開 ${siteUrl}`,
    `2. 用這個信箱（${to}）的 Google 帳號登入——用別的帳號登入會看不到邀請`,
    "3. 左側欄最上方的工作區選單裡會出現這個邀請，按「接受」即可",
  ].join("\n");
  const params = new URLSearchParams({
    view: "cm", fs: "1", to, su: `邀請你加入會議助手群組「${groupName}」`, body,
  });
  return `https://mail.google.com/mail/?${params}`;
}

export { inviteMailUrl, roleLabel };
