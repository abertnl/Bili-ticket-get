"""抢票状态、票档与策略结果的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .bili.errors import ResultKind


class GrabPhase(str, Enum):
    """自动平衡策略的运行阶段，用于调节节奏和复盘。"""

    IDLE = "idle"
    PREWARM = "prewarm"
    START_WAIT = "start_wait"
    START_BURST = "start_burst"
    CONGESTION_RETRY = "congestion_retry"
    RATE_LIMIT_COOLDOWN = "rate_limit_cooldown"
    SOLD_OUT_MONITOR = "sold_out_monitor"
    RETURN_BURST = "return_burst"
    CAPTCHA = "captcha"
    FINISHED = "finished"


@dataclass
class BurstDecision:
    """当前 burst 是否应该结束，以及复盘需要的计数。"""

    finished: bool = False
    reason: str = ""
    burst_attempts: int = 0
    burst_rate_limits: int = 0
    burst_sold_outs: int = 0


@dataclass
class BurstState:
    """一轮冲刺的限额和已发生结果。"""

    phase: GrabPhase
    max_create_attempts: int
    max_rate_limits: int
    max_sold_outs: int
    create_attempts: int = 0
    rate_limits: int = 0
    sold_outs: int = 0


class AttemptOutcome(str, Enum):
    """一次下单尝试后的状态机动作。"""

    RETRY = "retry"
    SOLD_OUT = "sold_out"
    STOP = "stop"


@dataclass
class AttemptResult:
    """一次下单尝试后的状态机动作与下一次重试节奏。"""

    outcome: AttemptOutcome
    retry_delay: float = 0.0
    retry_reason: str = ""
    code: int | None = None
    kind: ResultKind | None = None
    endpoint: str = ""


@dataclass
class TicketTarget:
    """本次尝试使用的票档快照。"""

    screen_id: int
    sku_id: int
    desc: str
    price: int
    sale_flag: str = ""
    num: int = 0
    order: int = 0


@dataclass
class GrabberStatus:
    """对外暴露的运行状态。"""

    running: bool = False
    attempts: int = 0
    monitor_checks: int = 0
    last_stock_status: str = ""
    last_code: int | None = None
    last_message: str = ""
    last_prepare_ms: int = 0
    last_create_ms: int = 0
    last_attempt_ms: int = 0
    avg_attempt_ms: int = 0
    network_errors: int = 0
    retry_reason: str = ""
    retry_delay_ms: int = 0
    success: bool = False
    order_id: str = ""
    payment_url: str = ""
    pay_qrcode_url: str = ""
    waiting_captcha: bool = False
    finished_reason: str = ""
    congestion_count: int = 0
    rate_limit_count: int = 0
    sold_out_count: int = 0
    consecutive_rate_limits: int = 0
    dynamic_interval_ms: int = 0
    phase: GrabPhase = GrabPhase.IDLE
    run_id: str = ""
    telemetry_path: str = ""
    effective_delay_ms: int = 0
    last_endpoint: str = ""
    effective_order_attempts: int = 0
    time_offset_ms: int = 0
    prewarm_ok: bool = False
    transport: str = ""
    target_sku_id: int = 0
    target_sku_desc: str = ""
    monitor_target_count: int = 0
    last_monitor_candidates: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()
