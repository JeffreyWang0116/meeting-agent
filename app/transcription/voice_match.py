"""VoiceMatcher：用會前錄的聲音樣本，把講者代號對到真實姓名。

與 SpeakerNamerAgent 互補，兩者判斷「誰是誰」的依據不同：
- SpeakerNamerAgent 讀**文字**線索（有人喊「請王委員發言」、某人自我介紹）
- 這裡比對**聲音**（會前每人錄一小段，模型拿它跟會議音訊對嗓音）

文字線索在沒人被點名的會議裡完全失效，聲紋則不受稱謂有無影響；反過來說，
聲紋在音質差或嗓音相近時會認錯，而文字線索是明確的。所以兩者併用，聲紋的
結果優先（它是使用者主動提供的第一手資訊），沒對到的再交給文字線索。

**為什麼不在轉錄階段直接標姓名**：轉錄 prompt 刻意規定一律輸出代號（見
_TRANSCRIBE_PROMPT），因為模型沒有跨段記憶，允許它自由選用姓名會讓同一個人
在不同段落標成不同東西；而且 SPEAKER_RE 只認代號格式，直接吐姓名會讓
speaker_label_ratio 歸零、觸發無謂的重試。姓名一律在最後一步統一填回。

比對結果只是「建議」：實際改寫仍走 apply_speaker_names，那裡會驗證行數、
時間標記、重複姓名。任何一步出錯都回空 dict——會前錄了樣本卻對不上時，
逐字稿維持代號仍然完全可用，不該讓一場開完的會議分析失敗。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from app.agents.speaker_namer_agent import is_safe_name
from app.gemini_keys import KeyPool, call_with_rotation
from app.transcription.segments import collect_speakers, speaker_of

_CODE_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


class VoiceMatchError(Exception):
    pass


def wait_until_active(client, handle, max_polls: int = 60, sleep=time.sleep):
    """輪詢到上傳的檔案就緒為止，回傳最新的 handle。

    音訊通常上傳即就緒，但大檔可能要等處理。還在 PROCESSING 就送出，Gemini
    會回 400——那既不是額度也不是暫時性錯誤，金鑰輪替救不了，最後被 match()
    的 except 吞掉，等於白付了上傳成本卻只拿到空結果。
    （GeminiTranscriber._transcribe_with_key 有同一道輪詢。）
    """
    for _ in range(max_polls):
        state = getattr(handle, "state", None)
        name = getattr(state, "name", str(state))  # 可能是列舉，也可能已是字串
        if name == "ACTIVE":
            return handle
        if name == "FAILED":
            raise VoiceMatchError("Gemini 檔案處理失敗，聲音樣本無法用於比對")
        sleep(1)
        handle = client.files.get(name=handle.name)
    return handle

_PROMPT_HEAD = (
    "你是會議錄音的講者辨識模組。接下來會給你兩種音訊：\n"
    "1. 幾位與會者各自的「聲音樣本」，每段樣本前面會註明是誰；\n"
    "2. 幾段實際的會議錄音，每段前面附上該段的逐字稿，逐字稿裡的講者以"
    "「講者A」「講者B」等代號標示。\n"
    "請比對嗓音，判斷每個代號實際上是哪一位與會者。\n"
)

_PROMPT_TAIL = (
    "\n務必遵守的規則：\n"
    "1. 只輸出一個 JSON 物件。不要 markdown 圍欄、不要任何額外說明文字。\n"
    "2. name 只能是上面聲音樣本中出現過的姓名，一字不差。不可以自己創造姓名，"
    "也不可以從逐字稿的內容去猜姓名——你的判斷依據只有嗓音。\n"
    "3. 嗓音明顯不像就不要對應。判斷不出來的代號直接省略，"
    "留著代號遠比安一個錯的名字好。\n"
    "4. 不同代號不可以對應到同一個人。\n"
    "5. 每筆在 evidence 用一句話說明嗓音的判斷依據。\n\n"
    "JSON 結構：\n"
    '{"speakers": [{"label": "講者A", "name": "王小明", "evidence": "判斷依據"}]}'
)


class VoiceMatcher:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-flash-lite-latest",
        generate=None,
        api_keys=None,
        on_call=None,
    ):
        self._pool = KeyPool(api_keys if api_keys else [api_key])
        # 每打一次 API 就回報一次，用量統計才算得到重試與換金鑰
        self._on_call = on_call
        self.api_key = self._pool.first
        self.model = model
        # 可注入 callable(parts) -> str，測試不需要真的呼叫 Gemini
        self._generate = generate or self._generate_with_gemini

    # ---- 對外介面 ----

    def match(self, enrollments: list[dict], evidence: list[dict]) -> dict[str, str]:
        """回傳 {講者代號: 姓名}，判斷不出來就省略該代號。

        enrollments：[{"name": 姓名, "path": 樣本音檔}]（依錄製順序）
        evidence：[{"path": 會議音檔, "transcript": 該段逐字稿}]

        任一邊是空的就直接回 {} 且完全不打 API——這是選用功能，
        使用者沒開就不該產生任何成本。
        """
        if not enrollments or not evidence:
            return {}
        try:
            raw = self._generate(self.build_parts(enrollments, evidence))
            mapping = _parse_mapping(raw)
        except Exception:
            return {}
        # 只認證據逐字稿裡真的出現過的代號。模型幻想一個「講者D」時，放它過關
        # 會讓下游 apply_speaker_names 整批放棄，連正確的那幾筆一起賠掉
        labels: list[str] = []
        for item in evidence:
            collect_speakers(item.get("transcript") or "", labels)
        return _sanitize(mapping, [e["name"] for e in enrollments], set(labels))

    def build_parts(
        self, enrollments: list[dict], evidence: list[dict]
    ) -> list[str | Path]:
        """組出交錯的內容：一段說明配一段音訊。

        全部音訊擠在一起、說明集中在最前面的話，模型無從得知第三個檔案是誰的
        樣本；交錯才建立得起「這段聲音＝這個名字」的對應。
        """
        parts: list[str | Path] = [_PROMPT_HEAD]
        for item in enrollments:
            parts.append(f"以下是與會者【{item['name']}】的聲音樣本：")
            parts.append(Path(item["path"]))
        for index, item in enumerate(evidence, 1):
            parts.append(
                f"以下是會議錄音片段 {index}，這段的逐字稿是：\n{item['transcript']}\n"
                "請比對這段錄音裡各個代號的嗓音："
            )
            parts.append(Path(item["path"]))
        parts.append(_PROMPT_TAIL)
        return parts

    # ---- 內部 ----

    def _generate_with_gemini(self, parts: list[str | Path]) -> str:
        if not self._pool:
            return '{"speakers": []}'  # 沒金鑰就等同「認不出任何人」
        return call_with_rotation(
            self._pool, lambda key: self._call_gemini(key, parts), on_call=self._on_call
        )

    def _call_gemini(self, key: str, parts: list[str | Path]) -> str:
        from google import genai

        client = genai.Client(api_key=key)
        # 上傳的檔案綁在該把 key 的專案底下，所以整組必須用同一把 key
        uploaded, contents = [], []
        try:
            for part in parts:
                if isinstance(part, Path):
                    handle = client.files.upload(file=str(part))
                    uploaded.append(handle)
                    # 一次要送 3~8 個檔，其中任何一個還沒就緒都會讓整次呼叫失敗
                    contents.append(wait_until_active(client, handle))
                else:
                    contents.append(part)
            response = client.models.generate_content(
                model=self.model,
                contents=contents,
                # temperature=0：這是比對判讀，不要創意
                config={"response_mime_type": "application/json", "temperature": 0.0},
            )
            return response.text or ""
        finally:
            # Files API 有儲存上限，用完即刪（與 GeminiTranscriber 同樣的紀律）
            for handle in uploaded:
                try:
                    client.files.delete(name=handle.name)
                except Exception:
                    pass


def _parse_mapping(raw: str) -> dict[str, str]:
    data = json.loads(_CODE_FENCE.sub("", (raw or "").strip()))
    items = data.get("speakers") if isinstance(data, dict) else data
    mapping: dict[str, str] = {}
    for item in items or []:
        if isinstance(item, dict) and item.get("label"):
            mapping[str(item["label"])] = str(item.get("name") or "")
    return mapping


def _sanitize(
    mapping: dict[str, str], enrolled: list[str], labels_present: set[str]
) -> dict[str, str]:
    """只留下「代號真的出現過、姓名是真的註冊過」的那些對應。

    模型即使被規則 2 約束，仍可能回傳沒註冊過的名字（從逐字稿內容猜的）。
    那正是這個功能要避免的事——聲紋比對的依據只能是嗓音，所以名冊之外的
    一律丟掉。逐條篩選而不是整批放棄：對到一個人也比全部維持代號有用。

    代號一律經 speaker_of 正規化，「講者 a」與「講者A」才會被當成同一個人；
    正規化後還必須真的在證據逐字稿裡出現過，否則下游會因為找不到那個代號而
    放棄整批對應。
    """
    known = set(enrolled)
    clean: dict[str, str] = {}
    for label, name in mapping.items():
        normalized = speaker_of(f"{label}：")
        if not normalized or normalized not in labels_present:
            continue  # 不是講者代號，或根本沒在這場會議出現過
        if name in known and is_safe_name(name):
            clean[normalized] = name
    # 同一個人對到兩個代號＝分不出哪個對，兩個都不要（下游 apply_speaker_names
    # 遇到重複姓名會整批放棄，在這裡先剔除才留得住其他正確的對應）
    seen: dict[str, int] = {}
    for name in clean.values():
        seen[name] = seen.get(name, 0) + 1
    return {k: v for k, v in clean.items() if seen[v] == 1}
