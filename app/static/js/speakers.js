/* ==================================================================
   講者標籤判斷：一行開頭的「XXX：」到底是不是講者
   ------------------------------------------------------------------
   原本是「冒號前 1~12 個字就算」，「重點：預算要保留」「我想請問部長，
   這個：」都會被畫成講者泡泡、還出現在講者改名清單裡。但也不能只認
   「講者A」這種代號——姓名對應後逐字稿就是「王小明：……」。規則：

   - 代號（講者A、Speaker 2、發言人3）一律算——與後端 segments.py 的 SPEAKER_RE 一致
   - 像名字的標籤（沒有標點、中文不夾空白）在同一份逐字稿出現兩次以上才算
   - 出席名單上的名字出現一次也算（只講一句「謝謝主席」的人也要掛得上名字）

   純函式、不碰 DOM：tests/test_speaker_labels_js.py 直接用 node 載入測試。
   ================================================================== */
const LABEL_RE = /^([^：:\n]{1,12})[：:]\s*/;
const CODE_RE = /^(講者|說話者|發言人|speaker)\s*([A-Za-z]|\d{1,2})$/i;
// 名字裡不會出現的字元：句讀、括號、引號
const NOT_NAME_RE = /[，。！？、；「」『』（）()[\]"'“”‘’,.!?;]/;

function looksLikeName(label) {
  if (NOT_NAME_RE.test(label)) return false;
  // 英文姓名可以有空白（Kevin Lin）；中文夾空白的多半是句子片段（今天 重點）
  return !(/\s/.test(label) && /[^\x00-\x7F]/.test(label));
}

// lines：已剝掉行首時間戳的各行。knownNames：出席者、已對應的講者姓名
function speakerLabels(lines, knownNames = []) {
  const known = new Set(knownNames.map(n => String(n).trim()).filter(Boolean));
  const counts = new Map();
  const labels = new Set();
  for (const line of lines) {
    const m = String(line).trim().match(LABEL_RE);
    if (!m) continue;
    const label = m[1].trim();
    if (CODE_RE.test(label) || known.has(label)) {
      labels.add(label);
    } else if (looksLikeName(label)) {
      counts.set(label, (counts.get(label) || 0) + 1);
    }
  }
  for (const [label, n] of counts) if (n >= 2) labels.add(label);
  return labels;
}

// 這一行若以認得的講者開頭，回傳 { speaker, rest }；否則 null（當作續行內容）
function matchSpeaker(line, labels) {
  const text = String(line).trim();
  const m = text.match(LABEL_RE);
  if (!m || !labels.has(m[1].trim())) return null;
  return { speaker: m[1].trim(), rest: text.slice(m[0].length) };
}

/* ------------------------------------------------------------------
   講者改名：系統一律標「講者A/B/C」，不從對話內容猜姓名——講者口中的
   「主席」「王委員」指的是別人，AI 會標錯。由使用者結束後自己替換；
   實際改寫在後端（app/speaker_rename.py），這裡只做輸入當下的檢查。
   ------------------------------------------------------------------ */
const MAX_NAME_LEN = 20;

// 新名字不合格的原因；合格回空字串。與後端 speaker_rename.name_problem 同一套
function speakerNameProblem(name) {
  const n = String(name || "").trim();
  if (!n) return "名字不可為空";
  if (n.length > MAX_NAME_LEN) return `名字最多 ${MAX_NAME_LEN} 個字`;
  if (/[：:\n[\]]/.test(n)) return "名字不可包含冒號、換行或方括號（會破壞逐字稿格式）";
  return "";
}

export { LABEL_RE, matchSpeaker, speakerLabels, speakerNameProblem };
