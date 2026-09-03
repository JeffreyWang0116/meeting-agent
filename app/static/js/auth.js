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
import { $, API_TOKEN_KEY, nativeFetch, setCredentialSource } from "./core.js";

const SDK = "https://www.gstatic.com/firebasejs/10.14.1";

let auth = null;
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

function showAccount(user, onSignOut) {
  $("accountName").textContent = user.displayName || user.email || "已登入";
  $("accountRow").hidden = false;
  $("accountSep").hidden = false;
  $("signOutBtn").onclick = onSignOut;
}

async function start() {
  const cfg = await config;
  if (!cfg.enabled) return;

  // 先擋住畫面：在確定「你是誰」之前，底下那些資料一眼都不該露出來
  const gate = $("loginGate");
  gate.hidden = false;

  let sdk;
  try {
    const [app, authMod] = await Promise.all([
      import(`${SDK}/firebase-app.js`),
      import(`${SDK}/firebase-auth.js`),
    ]);
    sdk = { ...app, ...authMod };
  } catch {
    $("loginError").textContent = "載入登入元件失敗，請檢查網路後重新整理。";
    return;
  }

  auth = sdk.getAuth(sdk.initializeApp({
    apiKey: cfg.apiKey,
    authDomain: cfg.authDomain,
    projectId: cfg.projectId,
  }));

  sdk.onAuthStateChanged(auth, user => {
    gate.hidden = !!user;
    if (!user) return;
    showAccount(user, () => sdk.signOut(auth).then(() => location.reload()));
    signedIn();
  });

  $("loginBtn").onclick = async () => {
    $("loginError").textContent = "";
    try {
      await sdk.signInWithPopup(auth, new sdk.GoogleAuthProvider());
    } catch (err) {
      // 使用者自己關掉登入視窗不算錯誤，不必嚇他
      if (err.code === "auth/popup-closed-by-user") return;
      $("loginError").textContent = `登入失敗：${err.message}`;
    }
  };
}

start();
