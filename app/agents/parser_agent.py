"""Parser Agent：輸入與解析模組。

負責把使用者貼上的文字（或轉錄產生的逐字稿）做雜訊過濾與格式標準化，
再交給 Decision Agent。
"""
from __future__ import annotations

import re

_ZERO_WIDTH = re.compile(r"[​‌‍﻿]")
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")


class ParserAgent:
    def parse(self, raw: str) -> str:
        if not raw or not raw.strip():
            raise ValueError("輸入內容是空的，請提供會議紀錄文字")

        text = raw.replace("\r\n", "\n").replace("\r", "\n")
        text = _ZERO_WIDTH.sub("", text)
        text = "\n".join(line.rstrip() for line in text.split("\n"))
        text = _EXCESS_BLANK_LINES.sub("\n\n", text)
        return text.strip()
