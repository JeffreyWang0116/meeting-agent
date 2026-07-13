"""Gemini 金鑰池：多把 API key 輪替（round-robin）。

免費層每把 key 每天只有少量請求配額。每次呼叫都推進到下一把 key
（第 1 次 key1、第 2 次 key2…循環），把配額平均分攤到所有 key。
單次呼叫若撞到 429（RESOURCE_EXHAUSTED），會在同一次呼叫內繼續往後
試，每把最多一次；全部爆掉才報錯。
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
        self._index = -1  # 第一次 next_key() 回傳第 0 把
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._keys)

    def __bool__(self) -> bool:
        return bool(self._keys)

    @property
    def first(self) -> str | None:
        """第一把（僅供 health 端點顯示，不影響輪替游標）。"""
        return self._keys[0] if self._keys else None

    def next_key(self) -> str | None:
        """round-robin：推進到下一把並回傳；空池回傳 None。"""
        with self._lock:
            if not self._keys:
                return None
            self._index = (self._index + 1) % len(self._keys)
            return self._keys[self._index]


def call_with_rotation(pool: KeyPool, fn: Callable[[str | None], T]) -> T:
    """每次呼叫先取下一把 key（round-robin）給 fn。

    fn(key) 撞到配額錯誤時，在同一次呼叫內續取下一把重試，每把最多一次；
    空池會以 None 呼叫一次，讓 fn 自己丟出「未設定金鑰」的友善錯誤；
    非配額錯誤（網路、格式…）直接往外拋，不再試其他 key。
    """
    if not pool:
        return fn(None)
    last: BaseException | None = None
    for _ in range(len(pool)):
        key = pool.next_key()
        try:
            return fn(key)
        except Exception as exc:
            if not is_quota_error(exc):
                raise
            last = exc
    raise last  # type: ignore[misc]  # 迴圈至少跑一次，到這裡必有例外
