"""请求节奏控制：AIMD 自适应间隔、限流冷却、网络退避与 prepare token 复用。"""

from __future__ import annotations

import time

from .config import AppConfig
from .grab_models import GrabberStatus, TicketTarget

# prepare token 最长复用时长（秒）：仅在拥堵可重试时短时复用，减半请求量
_TOKEN_REUSE_SECONDS = 2.5
# 连续触发限流后进入更长冷却。短间隔会把同一账号/IP持续压在 429 窗口里。
_RATE_LIMIT_COOLDOWN_AFTER = 3
_RATE_LIMIT_COOLDOWN_MAX_SECONDS = 30.0
_RATE_LIMIT_RECOVERY_RESPONSES = 10
_CONGESTION_DECREASE_SECONDS = 0.025


class PacingController:
    """单次运行的请求节奏。

    计数直接写入传入的 ``status``——两者都在每轮运行开始时重建，
    这样播报看到的数值与调用点严格同步。
    """

    def __init__(self, config: AppConfig, status: GrabberStatus) -> None:
        self._config = config
        self._status = status
        self.dynamic_interval = self.interval_seconds()
        self.rate_limit_recovery_remaining = 0
        self.consecutive_network_errors = 0

    # ---- 基础间隔 ----
    def interval_seconds(self) -> float:
        return self._config.interval_ms / 1000.0

    def aimd_floor(self) -> float:
        return max(self.interval_seconds(), 0.05)

    def aimd_ceiling(self) -> float:
        return max(self._config.max_interval_ms / 1000.0, self.aimd_floor())

    # ---- AIMD 自适应限速 ----
    def on_rate_limit(self) -> None:
        """收到限流信号（429/412）：乘性退避抬高动态间隔。"""

        self._status.rate_limit_count += 1
        self._status.consecutive_rate_limits += 1
        self.rate_limit_recovery_remaining = _RATE_LIMIT_RECOVERY_RESPONSES
        if self._config.adaptive_rate_enabled:
            self.dynamic_interval = min(self.dynamic_interval * 2.0, self.aimd_ceiling())
        self._status.dynamic_interval_ms = int(round(self.dynamic_interval * 1000))

    def on_retryable(self) -> None:
        """收到可重试（拥堵）响应：限流恢复期先稳住，之后缓慢加速。"""

        self._status.consecutive_rate_limits = 0
        if self._config.adaptive_rate_enabled:
            if self.rate_limit_recovery_remaining > 0:
                self.rate_limit_recovery_remaining -= 1
            else:
                self.dynamic_interval = max(
                    self.dynamic_interval - _CONGESTION_DECREASE_SECONDS,
                    self.aimd_floor(),
                )
        self._status.dynamic_interval_ms = int(round(self.dynamic_interval * 1000))

    def retry_delay(self) -> float:
        if not self._config.adaptive_rate_enabled:
            return self.interval_seconds()
        return self.dynamic_interval

    def rate_limit_retry_delay(self) -> float:
        self.on_rate_limit()
        base = self.dynamic_interval if self._config.adaptive_rate_enabled else self.interval_seconds()
        delay = max(base, self._config.rate_limit_backoff_ms / 1000.0)
        if self._status.consecutive_rate_limits >= _RATE_LIMIT_COOLDOWN_AFTER:
            cooldown = self._config.rate_limit_cooldown_ms / 1000.0
            cooldown_round = self._status.consecutive_rate_limits - _RATE_LIMIT_COOLDOWN_AFTER
            delay = max(delay, min(cooldown * (2**cooldown_round), _RATE_LIMIT_COOLDOWN_MAX_SECONDS))
        return delay

    # ---- 网络抖动退避 ----
    def on_network_error(self) -> None:
        self.consecutive_network_errors += 1

    def on_network_ok(self) -> None:
        self.consecutive_network_errors = 0

    def network_retry_delay(self) -> float:
        base = max(self.interval_seconds(), 0.2)
        multiplier = min(2 ** max(self.consecutive_network_errors - 1, 0), 8)
        return min(base * multiplier, self._config.network_backoff_max_ms / 1000.0)


class PrepareTokenCache:
    """prepare token 的短时复用缓存，按票档隔离。"""

    def __init__(self) -> None:
        self.token = ""
        self.ptoken = ""
        self.stored_at = 0.0
        self.sku_id = 0

    def clear(self) -> None:
        self.token = ""
        self.ptoken = ""
        self.stored_at = 0.0
        self.sku_id = 0

    def store(self, sku_id: int, token: str, ptoken: str) -> None:
        self.token = token
        self.ptoken = ptoken
        self.sku_id = sku_id
        self.stored_at = time.perf_counter()

    def reusable(self, target: TicketTarget) -> str:
        """返回可复用的 prepare token（仅拥堵重试且未过期），否则空串。"""

        if not self.token or self.sku_id != target.sku_id:
            return ""
        if time.perf_counter() - self.stored_at > _TOKEN_REUSE_SECONDS:
            self.clear()
            return ""
        return self.token
