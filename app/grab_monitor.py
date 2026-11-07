"""回流票监控：低频轮询项目详情，发现疑似可售的候选票档就交回冲刺。"""

from __future__ import annotations

import asyncio
from datetime import datetime
import re

from .bili import ticket
from .grab_context import GrabContext
from .grab_models import GrabPhase, TicketTarget

_MONITOR_RATE_LIMIT_REASON = "监控请求过频/风控拦截"


def rate_limit_code_from_exception(exc: Exception) -> int | None:
    match = re.search(r"\b(412|429)\b", str(exc))
    if not match:
        return None
    return int(match.group(1))


class ReturnTicketMonitor:
    """按 ``monitor_interval_ms`` 轮询，直到出现可售候选或到达截止时间。"""

    def __init__(self, ctx: GrabContext) -> None:
        self._ctx = ctx

    async def wait_for_return_ticket(self, deadline: datetime) -> list[TicketTarget]:
        ctx = self._ctx
        assert ctx.client is not None
        ctx.set_phase(GrabPhase.SOLD_OUT_MONITOR)
        interval = ctx.config.monitor_interval_ms / 1000.0
        while True:
            remaining = ctx.clock.remaining_seconds(deadline)
            if remaining <= 0:
                ctx.status.finished_reason = "达到监控截止时间"
                ctx.emit("已达到监控截止时间，停止", "warn")
                return []

            ctx.status.monitor_checks += 1
            ctx.status.last_endpoint = "project"
            try:
                project = await ticket.get_project(ctx.client, ctx.config.project_id)
                ctx.last_project = project
                available_targets = ctx.available_monitor_targets(project)
            except Exception as exc:  # noqa: BLE001
                await self._handle_check_error(exc, interval, remaining)
                continue

            if ctx.status.retry_reason == _MONITOR_RATE_LIMIT_REASON:
                ctx.clear_retry_status()
            ctx.status.last_stock_status = ctx.status.last_monitor_candidates
            ctx.recorder.record(
                ctx.status,
                "monitor_check",
                endpoint="project",
                code=0,
                stock_status=ctx.status.last_stock_status,
                outcome="available" if available_targets else "unavailable",
            )
            ctx.emit(
                f"[监控 {ctx.status.monitor_checks}] {ctx.status.last_stock_status}",
                "info",
            )
            ctx.emit_status()
            if available_targets:
                chosen = available_targets[0]
                ctx.emit(
                    (
                        "发现回流候选票档疑似可售："
                        f"{chosen.desc}（sku_id={chosen.sku_id}，库存提示 {chosen.num}），"
                        f"本轮监控候选 {len(available_targets)} 个"
                    ),
                    "success",
                )
                return available_targets

            await asyncio.sleep(min(interval, remaining))

    async def _handle_check_error(self, exc: Exception, interval: float, remaining: float) -> None:
        """轮询失败：限流走退避冷却，其余按监控间隔重试。"""

        ctx = self._ctx
        rate_limit_code = rate_limit_code_from_exception(exc)
        delay = min(interval, remaining)
        if rate_limit_code is not None:
            ctx.set_phase(GrabPhase.RATE_LIMIT_COOLDOWN)
            ctx.status.last_code = rate_limit_code
            ctx.status.last_message = f"HTTP {rate_limit_code}"
            delay = min(max(interval, ctx.pacing.rate_limit_retry_delay()), remaining)
            ctx.status.retry_reason = _MONITOR_RATE_LIMIT_REASON
            ctx.status.retry_delay_ms = int(round(delay * 1000))
        ctx.status.effective_delay_ms = int(round(delay * 1000))
        ctx.status.last_stock_status = f"监控异常: {exc}"
        ctx.recorder.record(
            ctx.status,
            "monitor_check",
            endpoint="project",
            code=rate_limit_code,
            delay_ms=ctx.status.effective_delay_ms,
            stock_status=ctx.status.last_stock_status,
            outcome="error",
        )
        ctx.emit(f"[监控 {ctx.status.monitor_checks}] 获取票档失败: {exc}", "warn")
        ctx.emit_status()
        await asyncio.sleep(delay)
        ctx.set_phase(GrabPhase.SOLD_OUT_MONITOR)

    async def refresh_return_targets(self) -> list[TicketTarget]:
        """冲刺中接口提示状态波动时，复检一次项目详情。"""

        ctx = self._ctx
        assert ctx.client is not None
        try:
            project = await ticket.get_project(ctx.client, ctx.config.project_id)
        except Exception as exc:  # noqa: BLE001
            ctx.emit(f"复检回流候选票档失败: {exc}", "warn")
            return []
        ctx.last_project = project
        try:
            targets = ctx.available_monitor_targets(project)
        except Exception as exc:  # noqa: BLE001
            ctx.emit(f"复检回流候选票档失败: {exc}", "warn")
            return []
        ctx.recorder.record(
            ctx.status,
            "monitor_refresh",
            endpoint="project",
            code=0,
            stock_status=ctx.status.last_monitor_candidates,
            outcome="available" if targets else "unavailable",
        )
        ctx.emit_status()
        return targets
