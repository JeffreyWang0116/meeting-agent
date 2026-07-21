"""Speaker Namer Agent：把逐字稿的講者代號換成真實姓名。

轉錄階段一律輸出「講者A/B/C」代號，不讓模型自由選用姓名——它沒有跨段記憶，
同一個人在聽得到稱謂的段落會標「王委員」、聽不到的段落標「講者A」，整份逐字稿
就混雜兩種標籤（這正是使用者回報的症狀）。

姓名改由這裡在最後一步補回：此時整份逐字稿都在手上，「請王委員發言」這種線索
可以用來判斷下一位發言者是誰，而且判斷只做一次，全場套用同一組對應。

**為什麼是「回傳對應表」而不是「回傳改好的逐字稿」**：與 CorrectorAgent 同一個
理由——整份重寫會讓模型順手刪贅字、合併句子、動到時間標記，時間軸一壞，會議
重點的點擊跳轉就跟著壞。模型只負責判斷「誰是誰」，改寫由本地執行並驗證結構。
"""
from __future__ import annotations

import json
import re

from app.gemini_keys import KeyPool, call_with_rotation
from app.transcription.segments import SPEAKER_RE, replace_speaker, speaker_of

_CODE_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")

# 行首時間標記（與 corrector_agent、前端 TIME_RE 對齊）
_TIME_MARKER = re.compile(r"\[\d{1,2}(?::\d{2}){1,2}\]")

# 姓名長度上限。真實姓名或職稱不會超過這個長度，超過多半是模型把整句話
# 當成名字回傳
MAX_NAME_LEN = 20

PROMPT_TEMPLATE = """你是會議逐字稿的講者辨識模組。以下逐字稿的講者以「講者A」「講者B」等代號標示，請根據對話內容判斷每個代號實際上是誰。

務必遵守的規則：
1. 只輸出一個 JSON 物件。不要 markdown 圍欄、不要任何額外說明文字。
2. 只在逐字稿裡有明確依據時才對應。可用的依據例如：有人喊「請王委員發言」，則下一位發言者就是王委員；或某人自我介紹、被點名、被稱呼職稱。
3. 沒有依據就不要放進結果。判斷不出來的代號直接省略，留著代號比編一個名字好得多。
4. name 只填姓名或職稱本身（例如「吳宗憲」「卓榮泰」「主席」），不要加冒號、不要加說明、最多 {max_len} 字。
5. 不要把代號對應成另一個代號。
6. 不同代號不可以對應到同一個人。
7. 每筆在 evidence 引用逐字稿中作為判斷依據的那句話。

JSON 結構：
{{
  "speakers": [
    {{"label": "講者A", "name": "真實姓名", "evidence": "判斷依據的原句"}}
  ]
}}

逐字稿：
---
{transcript}
---"""


class SpeakerNamerAgent:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-flash-lite-latest",
        generate=None,
        api_keys=None,
    ):
        self._pool = KeyPool(api_keys if api_keys else [api_key])
        self.api_key = self._pool.first
        self.model = model
        # 可注入 callable(prompt) -> str，測試時不需要真的呼叫 Gemini
        self._generate = generate or self._generate_with_gemini

    # ---- 對外介面 ----

    def name_speakers(self, transcript: str) -> tuple[str, list[dict]]:
        """回傳 (換上姓名的逐字稿, 實際套用的對應清單)。

        任何一步出錯都回傳原文＋空清單：代號本身是可用的，補姓名是加分項，
        不該擋住整個分析流程。
        """
        if not transcript or not transcript.strip():
            return transcript, []
        try:
            raw = self._generate(self.build_prompt(transcript))
            mapping = _parse_mapping(raw)
        except Exception:
            return transcript, []
        return apply_speaker_names(transcript, mapping)

    def build_prompt(self, transcript: str) -> str:
        return PROMPT_TEMPLATE.format(max_len=MAX_NAME_LEN, transcript=transcript)

    # ---- 內部 ----

    def _generate_with_gemini(self, prompt: str) -> str:
        if not self._pool:
            return '{"speakers": []}'  # 沒金鑰就等同「推不出任何姓名」
        return call_with_rotation(self._pool, lambda key: self._call_gemini(key, prompt))

    def _call_gemini(self, key: str, prompt: str) -> str:
        from google import genai

        client = genai.Client(api_key=key)
        response = client.models.generate_content(
            model=self.model,
            contents=prompt,
            # temperature=0：這是依據上下文的判讀，不要創意
            config={"response_mime_type": "application/json", "temperature": 0.0},
        )
        return response.text or ""


def _parse_mapping(raw: str) -> dict[str, str]:
    data = json.loads(_CODE_FENCE.sub("", (raw or "").strip()))
    items = data.get("speakers") if isinstance(data, dict) else data
    mapping: dict[str, str] = {}
    for item in items or []:
        if isinstance(item, dict) and item.get("label"):
            mapping[str(item["label"])] = str(item.get("name") or "")
    return mapping


def _is_safe_name(name: str) -> bool:
    if not name or len(name) > MAX_NAME_LEN:
        return False
    # 冒號會在行首造出第二個假標籤；換行會把一行拆成兩行破壞時間軸
    if "\n" in name or "：" in name or ":" in name:
        return False
    if "[" in name or "]" in name:
        return False
    # 對應成另一個代號不是命名，是重新編號
    return not SPEAKER_RE.match(f"{name}：")


def apply_speaker_names(
    transcript: str, mapping: dict[str, str]
) -> tuple[str, list[dict]]:
    """在本地把代號換成姓名，回傳 (新逐字稿, 實際生效的對應)。

    模型只負責判斷「誰是誰」，改寫由這裡執行——行結構、時間標記都在我們自己
    的控制下，模型無法間接改掉它們。只要有任何一筆對應不合格就整批放棄：
    一份「一半代號一半姓名」的逐字稿，比全部維持代號更難讀。
    """
    labels_present = {s for s in (speaker_of(ln) for ln in transcript.split("\n")) if s}
    safe: dict[str, str] = {}
    for label, name in mapping.items():
        if label not in labels_present or not _is_safe_name(name):
            return transcript, []
        safe[label] = name
    # 兩個代號指向同一人是模型不該擅自做的合併判斷
    if len(set(safe.values())) != len(safe):
        return transcript, []
    if not safe:
        return transcript, []

    counts: dict[str, int] = {}
    lines = []
    for line in transcript.split("\n"):
        label = speaker_of(line)
        if label in safe:
            line = replace_speaker(line, safe[label])
            counts[label] = counts.get(label, 0) + 1
        lines.append(line)
    text = "\n".join(lines)

    # 最終保險：行數與時間標記數量都不該變（與 apply_corrections 同一道防線）
    if text.count("\n") != transcript.count("\n") or len(
        _TIME_MARKER.findall(text)
    ) != len(_TIME_MARKER.findall(transcript)):
        return transcript, []
    return text, [
        {"label": label, "name": name, "count": counts.get(label, 0)}
        for label, name in safe.items()
    ]
