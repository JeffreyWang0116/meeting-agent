"""Gemini 金鑰池：多把 API key 輪替。

免費層每把 key 每天只有少量請求配額，撞到 429（RESOURCE_EXHAUSTED）時
自動換下一把、對同一個請求最多每把試一次。輪替後黏著在新 key 上，
後續請求不再浪費時間去打已爆額度的 key。
"""
from __future__ import annotations

import threading
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")


def is_quota_error(exc: BaseException) -> bool:
    text = str(exc)
    return "RESOURCE_EXHAUSTED" in text or "429" in text


class KeyPool:
    def __init__(self, keys: Iterable[str | None] | None):
        self._keys = [k.strip() for k in (keys or []) if k and k.strip()]
        self._index = 0
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._keys)

    def __bool__(self) -> bool:
        return bool(self._keys)

    @property
    def current(self) -> str | None:
        with self._lock:
            return self._keys[self._index] if self._keys else None

    def rotate(self, from_key: str | None = None) -> None:
        """換到下一把（環狀）。帶 from_key 可避免並發時把新 key 又換掉：
        只有當前 key 仍是撞到配額的那把時才真的輪替。"""
        with self._lock:
            if not self._keys:
                return
            if from_key is not None and self._keys[self._index] != from_key:
                return
            self._index = (self._index + 1) % len(self._keys)


def call_with_rotation(pool: KeyPool, fn: Callable[[str | None], T]) -> T:
    """fn(key) -> 結果；配額錯誤時輪替重試，每把 key 最多試一次。

    空池會以 None 呼叫一次，讓 fn 自己丟出「未設定金鑰」的友善錯誤。
    非配額錯誤（網路、格式…）直接往外拋，不輪替。
    """
    last: BaseException | None = None
    for _ in range(max(1, len(pool))):
        key = pool.current
        try:
            return fn(key)
        except Exception as exc:
            if not is_quota_error(exc) or len(pool) <= 1:
                raise
            last = exc
            pool.rotate(from_key=key)
    raise last  # type: ignore[misc]  # 迴圈至少跑一次，到這裡必有例外
