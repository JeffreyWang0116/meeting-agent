/* ==================================================================
   手機推播的純函式：為什麼開不起來、公鑰怎麼轉
   ------------------------------------------------------------------
   介面在 pushui.js。這裡不碰 DOM，node 可以直接測——開啟通知失敗時說
   錯一句話，使用者就直接放棄了，那段分支值得測。
   ================================================================== */

// 為什麼現在開不起來；回 null 代表沒問題。
// env：{ supported, ios, standalone, serverEnabled }
function blockedReason(env) {
  if (!env.serverEnabled) {
    return "這個站台還沒有設定推播金鑰，暫時無法開啟通知。";
  }
  if (!env.supported) {
    // iOS 只有「加到主畫面」的 PWA 收得到推播。瀏覽器自己只會說「不支援」，
    // 使用者看了會以為是這個 App 做不到，而不是少做了一個步驟
    if (env.ios && !env.standalone) {
      return "iPhone 要先把這個網站加到主畫面：Safari 下方的「分享」→「加入主畫面」，"
        + "再從主畫面的圖示打開，才能開啟通知。";
    }
    return "這個瀏覽器不支援推播通知。";
  }
  return null;
}

// pushManager.subscribe 只吃 Uint8Array，吃不下伺服器給的 base64url 字串；
// 而 atob 看到去掉結尾 = 的 base64url 會直接丟 InvalidCharacterError
function urlBase64ToUint8Array(base64url) {
  const padded = (base64url + "=".repeat((4 - (base64url.length % 4)) % 4))
    .replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(padded);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

export { blockedReason, urlBase64ToUint8Array };
