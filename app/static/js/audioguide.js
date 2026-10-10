/* ==================================================================
   收音防護：依平台給說明、偵測新插上的裝置、系統音源一直沒聲音
   ------------------------------------------------------------------
   使用者用 Discord 開會錄不到對方聲音，最常見的原因是 Discord 輸出到別的裝置，
   而 Chrome 分享「整個螢幕＋系統音訊」只錄 Windows 預設播放裝置——分享成功、
   音軌也在，錄到的卻是空白，沒有任何錯誤。所以除了事前說明，接上之後還要
   盯著音量，一直沒聲音就主動講。

   純函式、不碰 DOM：tests/test_audio_guide_js.py 直接用 node 載入測試。
   ================================================================== */

function detectPlatform(ua = "") {
  if (/iPhone|iPad|iPod/i.test(ua)) return "ios";
  if (/Android/i.test(ua)) return "android";
  if (/Windows/i.test(ua)) return "windows";
  if (/Macintosh|Mac OS X/i.test(ua)) return "mac";
  return "other";
}

const PHONE_NOTE = "手機瀏覽器收不到其他 App（Discord、LINE 通話等）的聲音，要錄線上會議請改用桌機版 Chrome／Edge。";

// 勾選「同時收錄耳機／系統音源」時顯示：分享對話框跳出來之前就知道該怎麼選
function systemAudioGuide(platform) {
  if (platform === "windows") {
    return "Discord、Teams 等桌面 App：分享時選「整個螢幕」並勾選「同時分享系統音訊」，"
      + "而且 Discord 的「設定 → 語音與視訊 → 輸出裝置」要設成「Default（預設）」——"
      + "Chrome 只錄 Windows 預設播放裝置，Discord 輸出到別的耳機就會錄到空白。"
      + "會議開在瀏覽器分頁的話，選那個分頁並勾選「分享分頁音訊」。";
  }
  if (platform === "mac") {
    return "macOS 上 Chrome 分享整個螢幕常常沒有系統聲音。Discord 請改用網頁版（discord.com/app），"
      + "分享時選那個分頁並勾選「分享分頁音訊」；一定要用 Discord 桌面版的話，"
      + "安裝免費的虛擬音效線 BlackHole，再從下方來源選單選它。";
  }
  if (platform === "ios" || platform === "android") return PHONE_NOTE;
  return "分享時選會議所在的分頁或整個螢幕，並勾選分享音訊。";
}

// 沒偵測到「立體聲混音」這類回放裝置時顯示：裝好就能直接錄、免每次分享畫面
function loopbackSetupHint(platform) {
  if (platform === "windows") {
    return "想免每次分享畫面：到「控制台 → 聲音 → 錄製」，在空白處按右鍵勾「顯示已停用的裝置」，"
      + "啟用「立體聲混音」後重新整理本頁；清單裡沒有這個裝置的話，安裝免費的 VB-CABLE。";
  }
  if (platform === "mac") {
    return "想免每次分享畫面：安裝免費的 BlackHole，並在「音訊 MIDI 設定」建立包含耳機與 BlackHole 的"
      + "「多重輸出裝置」、設為系統輸出，重新整理本頁後就能在來源選單選 BlackHole。";
  }
  return "";
}

// 系統音源接上了卻一直沒聲音時的提醒：先講最可能的原因
function silenceWarning(platform) {
  const head = "耳機／系統音源已經一段時間完全沒有聲音。如果對方其實有在講話，";
  if (platform === "windows") {
    return head + "最常見的原因是 Discord 的輸出裝置不是 Windows 預設播放裝置："
      + "到 Discord「設定 → 語音與視訊 → 輸出裝置」改成「Default（預設）」。"
      + "也請確認分享時選的是「整個螢幕」並勾選了「同時分享系統音訊」。";
  }
  if (platform === "mac") {
    return head + "macOS 上分享整個螢幕常常沒有系統聲音，請改用 Discord 網頁版並分享那個分頁"
      + "（勾選分享分頁音訊），或改用 BlackHole。";
  }
  return head + "請確認分享時有勾選分享音訊。";
}

// Chrome 在 Windows 上會多列兩個代表「預設」「通訊」裝置的別名，不是新裝置
const ALIAS_IDS = new Set(["", "default", "communications"]);

// 插拔後新出現的裝置名稱。沒授權麥克風前列舉不到名稱與 id，那種清單一律不算——
// 前一份清單也是沒名稱的話不能比，否則授權後第一次插拔會把每支麥克風都報成新裝置
function addedDevices(before, after) {
  before = before || [];
  if (before.length && !before.some(d => d.label)) return [];
  const known = new Set(before.map(d => d.deviceId));
  return (after || [])
    .filter(d => !ALIAS_IDS.has(d.deviceId) && d.label && !known.has(d.deviceId))
    .map(d => d.label);
}

// 記住的那支麥克風被拔掉或 id 變了（清過網站資料就會變）：改用系統預設再試。
// 權限問題換裝置也沒用；本來就用系統預設則退無可退
function shouldFallbackToDefaultMic(errorName, deviceId) {
  return Boolean(deviceId) && (errorName === "OverconstrainedError" || errorName === "NotFoundError");
}

// 連續 seconds 秒音量都低於 quietRms 就回報一次（之後不再重複提醒）。
// 中間只要有一下聲音就重新計時：對方一段時間沒講話是正常的
function createSilenceWatch({ seconds = 20, quietRms = 0.003 } = {}) {
  let quietSince = null, fired = false;
  return {
    push(rms, t) {
      if (rms >= quietRms) { quietSince = null; return false; }
      if (quietSince === null) quietSince = t;
      if (!fired && t - quietSince >= seconds) { fired = true; return true; }
      return false;
    },
  };
}

export { addedDevices, createSilenceWatch, detectPlatform, loopbackSetupHint, shouldFallbackToDefaultMic, silenceWarning, systemAudioGuide };
