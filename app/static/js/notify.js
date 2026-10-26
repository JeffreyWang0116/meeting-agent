// 瀏覽器通知。Android Chrome 不允許 new Notification()（直接丟 TypeError），
// 只能經由 service worker 發；有註冊就一律走那條，桌機沒有 SW 時才用建構子。
// 回傳有沒有真的發出去，呼叫端才能在使用者主動開啟時明講「這個瀏覽器發不出來」。
// env 參數只給測試注入假的 window。
async function showAppNotification(title, options, env = globalThis) {
  const N = env.Notification;
  if (!N || N.permission !== "granted") return false;
  try {
    const reg = await env.navigator?.serviceWorker?.getRegistration?.();
    if (reg) {
      await reg.showNotification(title, options);
      return true;
    }
  } catch { /* SW 發不出來就退回建構子 */ }
  try {
    new N(title, options);
    return true;
  } catch {
    return false;
  }
}

export { showAppNotification };
