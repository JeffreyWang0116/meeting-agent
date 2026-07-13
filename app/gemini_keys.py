"""Gemini 金鑰池：多把 API key 輪替（round-robin）＋暫時性錯誤重試。

免費層每把 key 每天只有少量請求配額。每次呼叫都推進到下一把 key
（第 1 次 key1、第 2 次 key2…循環），把配額平均分攤到所有 key。

單次呼叫內的錯誤處理：
- 429（RESOURCE_EXHAUSTED，配額爆）：立刻換下一把 key 續試；
  全部 key 都爆掉才報錯。
- 503（UNAVAILABLE，Google 端暫時過載）：指數退避（1s→2s→4s）後
  換下一把重試，最多 3 次；大檔轉錄常撞到這個，等一下通常就過。
- 其他錯誤（網路、格式…）：直接往外拋，不重試。
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")

# 503 重試次數與每次重試前的退避秒數
_TRANSIENT_RETRIES = 3
_BACKOFF_SECONDS = (1, 2, 4)


def is_quota_error(exc: BaseException) -> bool:
    text = str(exc)
    return "RESOURCE_EXHAUSTED" in text or "429" in text


def is_transient_error(exc: BaseException) -> bool:
    """Google 端暫時性過載（503 UNAVAILABLE）：稍等重試通常就會成功。"""
    text = str(exc)
    return "UNAVAILABLE" in text or "503" in text


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


def call_with_rotation(
    pool: KeyPool, fn: Callable[[str | None], T], *, sleep: Callable[[float], None] = time.sleep
) -> T:
    """每次呼叫先取下一把 key（round-robin）給 fn。

    fn(key) 撞到配額錯誤（429）時換下一把續試，直到所有不同的 key 都
    確認爆掉才放棄；撞到暫時性過載（503）時退避後重試，最多 3 次。
    空池會以 None 呼叫一次，讓 fn 自己丟出「未設定金鑰」的友善錯誤；
    其他錯誤直接往外拋，不再試。`sleep` 可注入以便測試不真的等待。
    """
    if not pool:
        return fn(None)
    exhausted: set[str | None] = set()  # 本次呼叫內已確認配額爆掉的 key
    transient_fails = 0
    while True:
        key = pool.next_key()
        try:
            return fn(key)
        except Exception as exc:
            if is_quota_error(exc):
                exhausted.add(key)
                if len(exhausted) == len(pool):
                    raise  # 所有 key 的配額都爆了
            elif is_transient_error(exc):
                transient_fails += 1
                if transient_fails > _TRANSIENT_RETRIES:
                    raise  # 退避重試仍然過載，放棄
                sleep(_BACKOFF_SECONDS[transient_fails - 1])
            else:
                raise
