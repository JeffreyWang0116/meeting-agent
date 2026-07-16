"""原子性檔案寫入。

直接 write_text 覆寫時，程序若在寫到一半被殺（部署重啟、斷電），整份
JSON 會損毀且無備援。改為先寫暫存檔再 os.replace（同一磁碟上為原子操作，
Windows 也可覆蓋既有檔），確保讀到的永遠是完整的舊檔或完整的新檔。
"""
from __future__ import annotations

import os
from pathlib import Path


def atomic_write_text(path: Path | str, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
