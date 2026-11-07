"""一次抢票运行的共享上下文：协作组件与公共的状态写入。

监控和下单执行各需要六七个依赖，逐个传参会让签名失控；这里把它们聚成一个
对象，同时收拢几处必须保持写入顺序的 status 同步逻辑。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import grab_targets
from .bili import ticket
from .config import AppConfig
from .grab_clock import GrabClock
from .grab_models import GrabPhase, GrabberStatus, TicketTarget
from .grab_pacing import PacingController, PrepareTokenCache
from .grab_reporting import RunRecorder


@dataclass
class GrabContext:
    config: AppConfig
    status: GrabberStatus
    recorder: RunRecorder
    pacing: PacingController
    tokens: PrepareTokenCache
    clock: GrabClock
    emit: Callable[..., None]
    emit_status: Callable[[], None]
    set_phase: Callable[[GrabPhase], None]
    client: Any = None
    last_project: ticket.Project | None = field(default=None)
    current_pay_money: int = 0

    async def sleep_with_telemetry(
        self,
        delay: float,
        delay_ms: int,
        outcome: str,
        message: str,
        *,
        emit_status: bool = True,
        **emit_fields: Any,
    ) -> None:
        """各类重试等待共用的记录与休眠：写复盘、播报、再 sleep。"""

        self.status.effective_delay_ms = delay_ms
        self.recorder.record(
            self.status,
            "retry_sleep",
            endpoint=self.status.last_endpoint,
            attempt=self.status.attempts,
            code=self.status.last_code,
            delay_ms=delay_ms,
            outcome=outcome,
        )
        self.emit(message, "info", **emit_fields)
        if emit_status:
            self.emit_status()
        await asyncio.sleep(delay)

    def set_active_target(self, target: TicketTarget) -> None:
        if self.status.target_sku_id and self.status.target_sku_id != target.sku_id:
            self.tokens.clear()
        self.status.target_sku_id = target.sku_id
        self.status.target_sku_desc = target.desc or str(target.sku_id)

    def clear_retry_status(self) -> None:
        self.status.retry_reason = ""
        self.status.retry_delay_ms = 0

    def monitor_targets(self, project: ticket.Project) -> list[TicketTarget]:
        """解析回流候选票档，并把数量同步到 status。"""

        targets = grab_targets.monitor_targets_from_project(self.config, project)
        self.status.monitor_target_count = len(targets)
        return targets

    def available_monitor_targets(self, project: ticket.Project) -> list[TicketTarget]:
        targets = self.monitor_targets(project)
        self.status.last_monitor_candidates = grab_targets.format_monitor_candidates(targets)
        return grab_targets.available_targets(self.config, targets)
