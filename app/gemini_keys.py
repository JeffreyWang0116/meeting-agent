"""Gemini 金鑰池：多把 API key 輪替（round-robin）＋暫時性錯誤重試。

免費層每把 key 每天只有少量請求配額。每次呼叫都推進到下一把 key
（第 1 次 key1、第 2 次 key2…循環），把配額平均分攤到所有 key。

單次呼叫內的錯誤處理：
- 429（RESOURCE_EXHAUSTED，配額爆）：立刻換下一把 key 續試；
  全部 key 都爆掉才報錯。
- 503（UNAVAILABLE，Google 端暫時過載）：指數退避後換下一把重試；
  大檔轉錄常撞到這個，等一下通常就過。
- 其他錯誤（網路、格式…）：直接往外拋，不重試。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable, Iterable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 每次 503 重試前的退避秒數（長度＝重試次數）。總窗口約兩分鐘：Google 的過載
# 尖峰動輒持續數十秒到數分鐘，舊的 (1,2,4) 只撐 7 秒，等於還沒等就放棄——
# 長檔轉到一半撞上，使用者白等十分鐘才看到一則 503。
_BACKOFF_SECONDS = (2, 5, 12, 30, 60)
# 退避加上 0~25% 的亂數。固定退避會讓同時撞牆的多個請求（多把 key、長檔的
# 相鄰分段）在同一刻一起重試，等於對同一顆過載的模型再次集中打擊
_JITTER_RATIO = 0.25


def _backoff_delay(attempt: int) -> float:
    base = _BACKOFF_SECONDS[attempt]
    return base + random.uniform(0, base * _JITTER_RATIO)


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
    pool: KeyPool,
    fn: Callable[[str | None], T],
    *,
    sleep: Callable[[float], None] = time.sleep,
    on_call: Callable[[], None] | None = None,
) -> T:
    """每次呼叫先取下一把 key（round-robin）給 fn。

    fn(key) 撞到配額錯誤（429）時換下一把續試，直到所有不同的 key 都
    確認爆掉才放棄；撞到暫時性過載（503）時退避後重試（見 _BACKOFF_SECONDS）。
    空池會以 None 呼叫一次，讓 fn 自己丟出「未設定金鑰」的友善錯誤；
    其他錯誤直接往外拋，不再試。`sleep` 可注入以便測試不真的等待。

    on_call()：每「實際打出去一次」就呼叫一次，換金鑰與退避重試都算。用量
    統計掛在這裡而不是端點層——端點層記的是「使用者按了幾次」，一個一小時
    的上傳按一次卻會打十幾次，兩者差一個數量級。
    """
    def attempt(key):
        if on_call:
            try:
                on_call()
            except Exception:  # 統計是附屬功能，壞掉不該讓轉錄跟著失敗
                pass
        return fn(key)

    if not pool:
        return attempt(None)
    exhausted: set[str | None] = set()  # 本次呼叫內已確認配額爆掉的 key
    transient_fails = 0
    while True:
        key = pool.next_key()
        try:
            return attempt(key)
        except Exception as exc:
            if is_quota_error(exc):
                exhausted.add(key)
                if len(exhausted) == len(pool):
                    raise  # 所有 key 的配額都爆了
            elif is_transient_error(exc):
                transient_fails += 1
                if transient_fails > len(_BACKOFF_SECONDS):
                    logger.warning(
                        "Google 端持續過載，退避重試 %d 次後放棄", len(_BACKOFF_SECONDS)
                    )
                    raise  # 退避重試仍然過載，放棄
                delay = _backoff_delay(transient_fails - 1)
                # 沉默地等兩分鐘，從外面看跟當掉一模一樣：進度條不動、logs 空白。
                # 這行是「它還在等」唯一的證據
                logger.warning(
                    "Google 端過載（503），等 %.1f 秒後重試（%d/%d）",
                    delay, transient_fails, len(_BACKOFF_SECONDS),
                )
                sleep(delay)
            else:
                raise
