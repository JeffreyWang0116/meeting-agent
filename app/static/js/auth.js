/* ==================================================================
   登入：Google 帳號（Firebase Auth）
   ------------------------------------------------------------------
   伺服器沒設 Firebase 設定時，這支等於不存在——維持原本的單人模式，
   core.js 的 API Token 輸入框照舊。設了就：

     1. 蓋上登入畫面，要求先用 Google 登入
     2. 登入後把 ID token 交給 core.js 的 fetch 攔截器，之後每個
        /api/* 請求都自動帶上（getIdToken 會在快過期時自動換新）
     3. 其餘十二支模組完全不必知道有登入這回事

   兩個時機很要命：

   - 憑證來源必須在「模組求值當下」就裝進 core，不能等抓完 /api/auth/config。
     其餘模組一載入就開始抓資料，晚一步裝好，第一輪請求會整批 401。
   - 反過來，也不能為了等登入而擋住模組載入：那樣只要有一步出錯（設定抓不到、
     CDN 連不上），整個 app 就是一片空白，極難查。所以改成讓請求在憑證供應者
     裡排隊——最壞情況只是資料一直轉圈，畫面還在、錯誤訊息看得到。

   Firebase SDK 用動態 import 從官方 CDN 取，維持本專案「沒有建置步驟」的
   做法；沒啟用登入的部署根本不會下載它。
   ================================================================== */
import { $, API_TOKEN_KEY, esc, nativeFetch, setCredentialSource, showError, switchWorkspace } from "./core.js";

const SDK = "https://www.gstatic.com/firebasejs/10.14.1";

let auth = null;
let sdk = null;                     // Firebase SDK 模組，登入啟用後才載入
let signedIn;                       // 登入完成才 resolve
const ready = new Promise(resolve => { signedIn = resolve; });

// 立刻開始抓，但不等它——下面的憑證供應者要在同一個同步回合內裝好
const config = loadAuthConfig();

async function loadAuthConfig() {
  // 用 nativeFetch：這支端點本身不需要認證，走攔截器會變成等自己
  try {
    const resp = await nativeFetch("/api/auth/config");
    return resp.ok ? await resp.json() : { enabled: false };
  } catch {
    return { enabled: false };      // 抓不到設定就當作沒啟用，不要讓 app 卡死
  }
}

setCredentialSource(async () => {
  const cfg = await config;         // 還不知道是哪種模式時，請求先在這裡等
  if (!cfg.enabled) return { token: localStorage.getItem(API_TOKEN_KEY) };
  await ready;                      // 等使用者登入
  return {
    firebase: true,
    token: auth?.currentUser ? await auth.currentUser.getIdToken() : null,
  };
});

// ---- 右上角頭像：帳號、切換帳號、登出 ----
function renderAvatar(user) {
  const guest = user.isAnonymous;
  const label = guest ? "訪客" : (user.displayName || user.email || "已登入");
  const img = $("avatarImg"), initial = $("avatarInitial");
  if (!guest && user.photoURL) {
    img.src = user.photoURL;
    img.hidden = false;
    initial.hidden = true;
  } else {
    img.hidden = true;
    initial.hidden = false;
    initial.textContent = guest ? "訪" : label.trim().charAt(0).toUpperCase();
  }
  $("avatarBtn").title = label;
  $("avatarMenu").innerHTML = guest
    ? `<div class="avatar-who"><b>訪客</b><span>資料會在離開訪客時刪除</span></div>
       <button class="ws-item" data-act="to-google">改用 Google 登入（訪客資料會刪除）</button>
       <button class="ws-item danger" data-act="leave-guest">離開訪客並刪除資料</button>`
    : `<div class="avatar-who"><b>${esc(user.displayName || "")}</b><span>${esc(user.email || "")}</span></div>
       <button class="ws-item" data-act="switch">切換帳號</button>
       <button class="ws-item" data-act="sign-out">登出</button>`;
  $("avatarWrap").hidden = false;
}

function closeAvatarMenu() {
  $("avatarMenu").hidden = true;
  $("avatarBtn").setAttribute("aria-expanded", "false");
}
$("avatarBtn").addEventListener("click", e => {
  e.stopPropagation();
  const open = $("avatarMenu").hidden;
  $("avatarMenu").hidden = !open;
  $("avatarBtn").setAttribute("aria-expanded", String(open));
});
document.addEventListener("click", e => {
  if (!$("avatarMenu").hidden && !e.target.closest("#avatarWrap")) closeAvatarMenu();
});

// 每次都讓使用者選帳號：不加這個，Google 會直接沿用瀏覽器目前登入的那個，「切換帳號」等於沒作用
function googleProvider() {
  const provider = new sdk.GoogleAuthProvider();
  provider.setCustomParameters({ prompt: "select_account" });
  return provider;
}

async function signInWithGoogle() {
  try {
    await sdk.signInWithPopup(auth, googleProvider());
    return true;
  } catch (err) {
    if (err.code === "auth/popup-closed-by-user" || err.code === "auth/cancelled-popup-request") return false;
    throw err;
  }
}

$("avatarMenu").addEventListener("click", async e => {
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (!act) return;
  closeAvatarMenu();
  try {
    if (act === "switch") {
      await signInWithGoogle();
    } else if (act === "sign-out") {
      await sdk.signOut(auth);
    } else if (act === "to-google") {
      if (!confirm("改用 Google 登入後，這個訪客的會議與任務會刪除。要繼續嗎？")) return;
      // 先記住訪客的憑證：登入 Google 之後目前使用者就換人了，拿不到訪客的 token
      const guestToken = await auth.currentUser.getIdToken();
      if (!(await signInWithGoogle())) return;  // 取消登入就維持訪客，資料不刪
      await nativeFetch("/api/guest/data", { method: "DELETE", headers: { Authorization: `Bearer ${guestToken}` } });
    } else if (act === "leave-guest") {
      if (!confirm("離開後這個訪客的會議與任務都會刪除，無法復原。要離開嗎？")) return;
      const resp = await fetch("/api/guest/data", { method: "DELETE" });
      if (!resp.ok) throw new Error(`刪除訪客資料失敗（${resp.status}）`);
      await sdk.signOut(auth);
    }
  } catch (err) { showError("帳號操作失敗：" + err.message); }
});

function showLoginOptions(show) {
  $("loginLoading").hidden = show;
  $("loginOptions").hidden = !show;
}

let knownUid = null;  // 換了帳號（切換帳號、登出後換人登入）就要整批重抓資料

async function start() {
  const cfg = await config;
  if (!cfg.enabled) return;

  // 先擋住畫面：在確定「你是誰」之前，底下那些資料一眼都不該露出來。
  // 但先顯示「載入中」而不是登入按鈕——已登入的人重新整理時只是要等 SDK 恢復狀態
  const gate = $("loginGate");
  gate.hidden = false;
  showLoginOptions(false);
  $("guestBtn").hidden = !cfg.guestEnabled;
  $("guestNote").hidden = !cfg.guestEnabled;

  try {
    const [app, authMod] = await Promise.all([
      import(`${SDK}/firebase-app.js`),
      import(`${SDK}/firebase-auth.js`),
    ]);
    sdk = { ...app, ...authMod };
  } catch {
    showLoginOptions(false);
    $("loginLoading").hidden = true;
    $("loginError").textContent = "載入登入元件失敗，請檢查網路後重新整理。";
    return;
  }

  auth = sdk.getAuth(sdk.initializeApp({
    apiKey: cfg.apiKey,
    authDomain: cfg.authDomain,
    projectId: cfg.projectId,
  }));

  sdk.onAuthStateChanged(auth, user => {
    if (!user) {
      gate.hidden = false;
      showLoginOptions(true);
      $("avatarWrap").hidden = true;
      return;
    }
    gate.hidden = true;
    renderAvatar(user);
    const switched = knownUid !== null && knownUid !== user.uid;
    knownUid = user.uid;
    signedIn();
    // 換了人：畫面上全是上一個帳號的資料。不整頁重新載入（不保存登入狀態的瀏覽器
    // 重新載入就被登出了），改成切回個人工作區、讓各模組用新帳號的憑證重抓
    if (switched) {
      window.dispatchEvent(new CustomEvent("accountchange"));
      switchWorkspace("");
    }
  });

  $("loginBtn").onclick = async () => {
    $("loginError").textContent = "";
    try {
      await signInWithGoogle();
    } catch (err) {
      $("loginError").textContent = `登入失敗：${err.message}`;
    }
  };

  $("guestBtn").onclick = async () => {
    $("loginError").textContent = "";
    try {
      await sdk.signInAnonymously(auth);
    } catch (err) {
      $("loginError").textContent = err.code === "auth/operation-not-allowed" || err.code === "auth/admin-restricted-operation"
        ? "網站管理者還沒在 Firebase 開啟匿名登入，暫時只能用 Google 登入。"
        : `訪客登入失敗：${err.message}`;
    }
  };
}

// ---- Google 行事曆授權 ----
// 刻意不把 calendar scope 併進登入：登入畫面多一句「存取你的 Google 日曆」會
// 嚇退根本用不到這功能的人，而登入是每個人的必經之路。改成使用者真的按下
// 「加入行事曆」時才要權限。
//
// 另一個非要延後不可的理由：這把 OAuth access token 只在 popup 回傳的當下拿
// 得到，Firebase 不會保存它。重新整理後 onAuthStateChanged 只還你 ID token，
// 行事曆權杖是拿不回來的——所以本來就只能在使用者操作的當下現拿。
const CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events";

// 訪客（匿名登入）沒有 Google 帳號，加不了 Google 行事曆
function googleSignedIn() {
  return !!auth?.currentUser && !auth.currentUser.isAnonymous;
}

// 登入完成後的身分（沒開登入時永遠不會 resolve，呼叫端要先確認有開）
function signedInUser() {
  return ready.then(() => (auth?.currentUser
    ? { uid: auth.currentUser.uid, guest: auth.currentUser.isAnonymous }
    : null));
}

async function requestCalendarToken() {
  if (!sdk || !auth?.currentUser) return null;
  const provider = new sdk.GoogleAuthProvider();
  provider.addScope(CALENDAR_SCOPE);
  // 用 reauthenticate 而不是 signInWithPopup：使用者在 popup 裡選到另一個
  // Google 帳號時，前者丟 auth/user-mismatch，後者會默默把整個 app 切換成
  // 另一個帳號的資料——畫面上毫無提示，只會發現東西全不見了。
  const result = await sdk.reauthenticateWithPopup(auth.currentUser, provider);
  return sdk.GoogleAuthProvider.credentialFromResult(result)?.accessToken || null;
}

start();

export { googleSignedIn, requestCalendarToken, signedInUser };
