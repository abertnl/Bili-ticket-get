"""开抢计时：服务端时间偏移、预热等待与开抢倒计时。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
import time

from .config import AppConfig
from .grab_models import GrabPhase
from .time_utils import parse_local_datetime


class GrabClock:
    """带服务端偏移的时钟，以及两段等待循环。

    阶段切换通过 ``set_phase`` 回调交回调用方，等待过程中的播报走 ``emit``。
    """

    def __init__(
        self,
        config: AppConfig,
        emit: Callable[..., None],
        set_phase: Callable[[GrabPhase], None],
    ) -> None:
        self._config = config
        self._emit = emit
        self._set_phase = set_phase
        self.offset_seconds = 0.0

    def set_offset_ms(self, offset_ms: int) -> None:
        self.offset_seconds = offset_ms / 1000.0

    def parse(self, value: str, label: str) -> datetime | None:
        try:
            return parse_local_datetime(value)
        except ValueError as exc:
            raise RuntimeError(f"{label}格式无效: {value}") from exc

    def now(self, target: datetime) -> datetime:
        return datetime.now(tz=target.tzinfo) + timedelta(seconds=self.offset_seconds)

    def remaining_seconds(self, deadline: datetime) -> float:
        return (deadline - self.now(deadline)).total_seconds()

    # ---- 等待循环 ----
    async def wait_until_prewarm(self, target: datetime | None) -> None:
        """粗粒度等到预热窗口开启，期间按剩余时长拉长播报间隔。"""

        if target is None:
            return
        self._set_phase(GrabPhase.PREWARM)
        prewarm_at = target - timedelta(seconds=self._config.prewarm_seconds)
        while True:
            now = self.now(target)
            remaining_to_prewarm = (prewarm_at - now).total_seconds()
            if remaining_to_prewarm <= 0:
                return
            remaining_to_start = (target - now).total_seconds()
            self._emit(f"距开抢还有 {remaining_to_start:.1f}s，等待预热", "info")
            await asyncio.sleep(min(remaining_to_prewarm, self.coarse_wait_step(remaining_to_start)))

    async def wait_until_start(self, target: datetime | None = None) -> None:
        """倒计时到开抢点；最后 3 秒切到毫秒级轮询。"""

        if target is None:
            target = self.parse(self._config.start_time, "开抢时间")
        if target is None:
            return
        self._set_phase(GrabPhase.START_WAIT)
        initial_remaining = (target - self.now(target)).total_seconds()
        if initial_remaining <= 0:
            return
        end_at = time.perf_counter() + initial_remaining
        last_logged_second: int | None = None
        while True:
            remaining = end_at - time.perf_counter()
            if remaining <= 0:
                break
            current_second = int(remaining)
            if remaining <= 3.0:
                if last_logged_second != 0:
                    self._emit("进入毫秒级倒计时，等待开抢点", "info")
                    last_logged_second = 0
                step = 0.05
            elif current_second != last_logged_second:
                self._emit(f"距开抢还有 {remaining:.1f}s", "info")
                last_logged_second = current_second
                step = 0.5 if remaining <= 10.0 else 1.0
            else:
                step = 0.5 if remaining <= 10.0 else 1.0
            await asyncio.sleep(min(remaining, step))

    def coarse_wait_step(self, remaining_to_start: float) -> float:
        if remaining_to_start > 300:
            return 60.0
        if remaining_to_start > 60:
            return 10.0
        if remaining_to_start > 10:
            return 2.0
        return 1.0
