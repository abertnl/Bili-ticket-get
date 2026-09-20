"""抢票状态机：定时开抢、请求间隔、重试、风控接入、抢中停止。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from . import grab_targets
# 现有测试通过 ``app.grabber.notify`` 定位推送模块，保留该导入入口。
from . import notify  # noqa: F401
from .bili import ticket
from .bili.captcha import ManualSolver, build_solver
from .bili.client import BiliClient
from .bili.errors import ResultKind
from .bili.risk import RiskError, RiskHandler
from .config import AppConfig
from .grab_clock import GrabClock
from .grab_context import GrabContext
from .grab_monitor import ReturnTicketMonitor
from .grab_order import OrderExecutor, has_pending_order
from .grab_models import AttemptOutcome, AttemptResult, BurstDecision, BurstState, GrabPhase, GrabberStatus, TicketTarget
from .grab_pacing import PacingController, PrepareTokenCache
from .grab_strategy import BalancedStrategyController
from .grab_reporting import RunRecorder

# 保持原有类型导入入口。
__all__ = [
    "Grabber", "GrabPhase", "GrabberStatus", "AttemptOutcome", "AttemptResult",
    "BurstDecision", "BurstState", "TicketTarget", "BalancedStrategyController",
]

# 日志事件回调：接收一个 dict 事件（type/level/message/...）
EventCallback = Callable[[dict[str, Any]], None]

TELEMETRY_DIR = Path("runtime/grab-runs")


class Grabber:
    """单账号抢票任务。"""

    def __init__(self, config: AppConfig, on_event: EventCallback | None = None) -> None:
        self.config = config
        self.on_event = on_event or (lambda e: None)
        self.solver = build_solver(config.captcha_mode, config.rrocr_token)
        self.risk = RiskHandler(self.solver)
        self._task: asyncio.Task | None = None
        self._reset_run_state()

    # ---- 日志 ----
    def _emit(self, message: str, level: str = "info", **extra: Any) -> None:
        event = {
            "type": "log",
            "level": level,
            "message": message,
            "time": datetime.now().strftime("%H:%M:%S"),
            **extra,
        }
        self.on_event(event)

    def _emit_status(self) -> None:
        self.on_event({"type": "status", **self.status.to_dict()})

    def _set_phase(self, phase: GrabPhase) -> None:
        self.status.phase = self.strategy.set_phase(phase)

    def _start_burst(self, phase: GrabPhase) -> None:
        self.strategy.start_burst(phase, self.config.sold_out_burst_attempts)
        self.status.phase = phase

    def report(self) -> dict[str, Any]:
        return self._recorder.report(self.status)

    @property
    def _client(self) -> BiliClient | None:
        """HTTP 客户端由上下文持有，监控与下单共用同一个实例。"""

        return self._ctx.client

    @_client.setter
    def _client(self, value: BiliClient | None) -> None:
        self._ctx.client = value

    # ---- 人工验证码交互（供 server 调用） ----
    def captcha_pending(self) -> dict[str, str] | None:
        if isinstance(self.solver, ManualSolver):
            cur = self.solver.current()
            if cur:
                return {"gt": cur.gt, "challenge": cur.challenge}
        return None

    def submit_captcha(self, validate: str, seccode: str) -> bool:
        if isinstance(self.solver, ManualSolver):
            return self.solver.submit(validate, seccode)
        return False

    # ---- 生命周期 ----
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.status.running = False
        self._emit_status()

    # ---- 主流程 ----
    async def _resolve_buyers(self) -> list[ticket.Buyer]:
        assert self._client is not None
        wanted_ids = list(dict.fromkeys(self.config.buyer_ids))
        if len(wanted_ids) != len(self.config.buyer_ids):
            raise RuntimeError("购票人不能重复选择")
        all_buyers = await ticket.get_buyers(self._client)
        wanted = set(wanted_ids)
        chosen = [b for b in all_buyers if b.buyer_id in wanted]
        if len(chosen) != len(wanted):
            found = {b.buyer_id for b in chosen}
            missing = [str(buyer_id) for buyer_id in wanted_ids if buyer_id not in found]
            raise RuntimeError(f"未匹配到购票人: {', '.join(missing)}，请重新加载购票人")
        if len(chosen) != self.config.count:
            raise RuntimeError("购票人数必须与购买数量一致")
        return chosen

    async def _resolve_price(self) -> int:
        assert self._client is not None
        try:
            project = await ticket.get_project(self._client, self.config.project_id)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"获取演出信息失败: {exc}") from exc
        self._ctx.last_project = project
        target = grab_targets.find_target(self.config, project, self.config.sku_id, 0)
        self._primary_target = target
        return target.price

    async def _resolve_prewarm_data(self) -> tuple[list[ticket.Buyer], int]:
        """预热阶段解析购票人与票价；遇瞬态错误时有限次重试，配置类错误立即抛出。

        日志中曾出现单次 ``获取演出信息失败: Expecting value`` 让整个抢票崩溃，这里在
        预热窗口内重试若干次，避免一次网络/响应抖动让任务中断。
        """

        max_resolve = 3
        for attempt in range(1, max_resolve + 1):
            try:
                buyers, pay_money = await asyncio.gather(
                    self._resolve_buyers(), self._resolve_price()
                )
                if pay_money <= 0:
                    raise RuntimeError("票价无效，请重新加载演出并选择票档")
                return buyers, pay_money
            except Exception as exc:  # noqa: BLE001
                if _is_permanent_resolve_error(exc) or attempt >= max_resolve:
                    raise
                self._emit(f"预热数据获取失败，重试（{attempt}/{max_resolve}）: {exc}", "warn")
                await asyncio.sleep(min(1.0 * attempt, 3.0))
        raise RuntimeError("预热数据获取失败")  # 理论不可达，满足类型检查

    def _clear_retry_status(self) -> None:
        self._ctx.clear_retry_status()

    async def _sleep_before_retry(self, result: AttemptResult) -> None:
        if result.retry_delay <= 0:
            return
        delay_ms = self.status.retry_delay_ms
        await self._ctx.sleep_with_telemetry(
            result.retry_delay,
            delay_ms,
            result.retry_reason,
            f"等待 {delay_ms}ms 后重试（{result.retry_reason}）",
            retry_delay_ms=delay_ms,
            retry_reason=result.retry_reason,
        )

    async def _sleep_continue_burst(self) -> None:
        """库存不足但尚未达到回监控的冲刺轮数时，给一个自适应间隔再继续冲刺。"""

        delay = self._pacing.retry_delay()
        if delay <= 0:
            return
        delay_ms = int(round(delay * 1000))
        await self._ctx.sleep_with_telemetry(
            delay,
            delay_ms,
            "库存不足继续冲刺",
            f"库存不足，继续冲刺（{delay_ms}ms 后重试）",
            emit_status=False,
        )

    def _record_burst_finish(self, decision: BurstDecision) -> None:
        self._recorder.record(
            self.status,
            "burst_finish",
            endpoint=self.status.last_endpoint,
            attempt=self.status.attempts,
            code=self.status.last_code,
            outcome=decision.reason,
            burst_attempts=decision.burst_attempts,
            burst_rate_limits=decision.burst_rate_limits,
            burst_sold_outs=decision.burst_sold_outs,
        )
        self._emit(
            (
                "本轮冲刺结束："
                f"{decision.reason}，下单 {decision.burst_attempts} 次，"
                f"限流 {decision.burst_rate_limits} 次，库存不足 {decision.burst_sold_outs} 次"
            ),
            "info",
        )

    def _burst_pause_seconds(self, decision: BurstDecision, result: AttemptResult) -> float:
        pause = self.config.monitor_interval_ms / 1000.0
        if decision.reason == "rate_limit_wall":
            pause = max(pause, result.retry_delay)
        return pause

    async def _sleep_between_bursts(self, delay: float, deadline: datetime | None) -> None:
        if delay <= 0:
            return
        if deadline is not None:
            remaining = self._clock.remaining_seconds(deadline)
            if remaining <= 0:
                return
            delay = min(delay, remaining)

        delay_ms = int(round(delay * 1000))
        await self._ctx.sleep_with_telemetry(
            delay,
            delay_ms,
            "burst 间隔等待",
            f"等待 {delay_ms}ms 后进入下一轮监控/冲刺",
        )

    def _reset_run_state(self, *, running: bool = False) -> None:
        """初始化每次运行拥有的状态，避免上一次运行的数据残留。"""

        self.status = GrabberStatus(running=running)
        self.strategy = BalancedStrategyController()
        self._attempt_ms_total = 0
        self._pacing = PacingController(self.config, self.status)
        if running:
            self.status.dynamic_interval_ms = int(round(self._pacing.dynamic_interval * 1000))
        self._tokens = PrepareTokenCache()
        self._clock = GrabClock(self.config, self._emit, self._set_phase)
        self._recorder = RunRecorder()
        self._ctx = GrabContext(
            config=self.config,
            status=self.status,
            recorder=self._recorder,
            pacing=self._pacing,
            tokens=self._tokens,
            clock=self._clock,
            emit=self._emit,
            emit_status=self._emit_status,
            set_phase=self._set_phase,
        )
        self._monitor = ReturnTicketMonitor(self._ctx)
        self._executor = OrderExecutor(self._ctx, self._invoke_risk_handler)
        self._primary_target: TicketTarget | None = None

    async def _prepare_run(self) -> tuple[list[ticket.Buyer], TicketTarget, datetime | None]:
        """预热连接、解析购票信息，然后等待开抢。"""

        assert self._client is not None
        start_target = self._clock.parse(self.config.start_time, "开抢时间")
        if self.config.time_sync_enabled and hasattr(self._client, "sync_server_time"):
            try:
                offset_ms = await self._client.sync_server_time()
            except Exception as exc:  # noqa: BLE001
                offset_ms = 0
                self._emit(f"服务端时间同步失败，将使用本机时间: {exc}", "warn")
            else:
                self._emit(f"服务端时间偏移估算 {offset_ms}ms", "info")
            self.status.time_offset_ms = offset_ms
            self._clock.set_offset_ms(offset_ms)
            self._emit_status()

        self._set_phase(GrabPhase.PREWARM)
        await self._wait_until_prewarm(start_target)
        self._emit("开始抢票预热", "info")
        if self.config.connection_prewarm_enabled and hasattr(self._client, "prewarm_connection"):
            try:
                self.status.prewarm_ok = await self._client.prewarm_connection()
            except Exception as exc:  # noqa: BLE001
                self.status.prewarm_ok = False
                self._emit(f"会员购连接预热异常，将继续抢票: {exc}", "warn")
                self._emit_status()
            else:
                if self.status.prewarm_ok:
                    self._emit(f"会员购连接已预热（{self.status.transport or 'http'}）", "info")
                else:
                    self._emit("会员购连接预热失败，将继续抢票", "warn")
                self._emit_status()

        if await self._client.gen_bili_ticket():
            self._emit("bili_ticket 已刷新", "info")
        else:
            self._emit("bili_ticket 生成失败，将继续抢票", "warn")

        deadline: datetime | None = None
        if self.config.return_monitor_enabled:
            deadline = self._clock.parse(self.config.monitor_end_time, "监控截止时间")
            if deadline is None:
                raise RuntimeError("启用回流票监控时必须设置监控截止时间")
            if self._clock.now(deadline) >= deadline:
                raise RuntimeError("监控截止时间已过期")

        buyers, pay_money = await self._resolve_prewarm_data()
        self._ctx.current_pay_money = pay_money
        primary_target = self._primary_target or TicketTarget(
            screen_id=self.config.screen_id,
            sku_id=self.config.sku_id,
            desc=f"sku_id={self.config.sku_id}",
            price=pay_money,
        )
        self._ctx.set_active_target(primary_target)
        if self.config.return_monitor_enabled:
            project = self._ctx.last_project
            if project is None:
                assert self._client is not None
                project = await ticket.get_project(self._client, self.config.project_id)
                self._ctx.last_project = project
            monitor_targets = self._ctx.monitor_targets(project)
            self.status.last_monitor_candidates = grab_targets.format_monitor_candidates(monitor_targets)
            self._emit(
                f"回流监控候选票档 {len(monitor_targets)} 个：{self.status.last_monitor_candidates}",
                "info",
            )
            self._emit_status()
        self._emit(
            (
                f"购票人 {len(buyers)} 人，首发票档 {primary_target.desc} "
                f"票价 {primary_target.price / 100:.2f} 元"
            ),
            "info",
        )

        await self._wait_until_start(start_target)
        self._set_phase(GrabPhase.START_BURST)
        self._emit("开始抢票", "info")
        if self.config.return_monitor_enabled:
            self._emit("回流票监控已启用；先对首发票档短冲刺，之后低频监控候选票档", "info")

        return buyers, primary_target, deadline

    async def _run_order_loop(
        self,
        buyers: list[ticket.Buyer],
        primary_target: TicketTarget,
        deadline: datetime | None,
    ) -> None:
        """执行首发冲刺与回流监控，直到停止或达到尝试上限。"""

        extra_params: dict[str, Any] = {}
        initial_burst_pending = self.config.return_monitor_enabled
        next_burst_pause = 0.0

        while self.status.attempts < self.config.max_attempts:
            burst_targets = [primary_target]
            if self.config.return_monitor_enabled:
                if initial_burst_pending:
                    initial_burst_pending = False
                    self._start_burst(GrabPhase.START_BURST)
                else:
                    assert deadline is not None
                    if next_burst_pause > 0:
                        await self._sleep_between_bursts(next_burst_pause, deadline)
                        next_burst_pause = 0.0
                    monitor_result = await self._wait_for_return_ticket(deadline)
                    if not monitor_result:
                        return
                    burst_targets = list(monitor_result)
                    self._start_burst(GrabPhase.RETURN_BURST)
            else:
                if next_burst_pause > 0:
                    await self._sleep_between_bursts(next_burst_pause, None)
                    next_burst_pause = 0.0
                self._start_burst(GrabPhase.START_BURST)

            target_index = 0
            refreshed_return_targets = False
            while self.status.attempts < self.config.max_attempts:
                target = burst_targets[min(target_index, len(burst_targets) - 1)]
                result = await self._attempt_order(buyers, target, extra_params)
                if result.outcome is AttemptOutcome.STOP:
                    return
                return_burst_active = (
                    self.config.return_monitor_enabled and self.strategy.in_return_burst()
                )
                burst_decision = self.strategy.record_attempt(result)
                sold_out_result = (
                    result.outcome is AttemptOutcome.SOLD_OUT or result.kind is ResultKind.SOLD_OUT
                )
                if sold_out_result and return_burst_active:
                    if target_index + 1 < len(burst_targets):
                        previous = target
                        target_index += 1
                        next_target = burst_targets[target_index]
                        self._emit(
                            (
                                f"{previous.desc} 库存不足，改试候选票档 "
                                f"{next_target.desc}（sku_id={next_target.sku_id}）"
                            ),
                            "warn",
                        )
                        continue
                    if not burst_decision.finished and self.status.attempts < self.config.max_attempts:
                        await self._sleep_continue_burst()
                        continue
                if result.code == 100001 and return_burst_active and not refreshed_return_targets:
                    refreshed_return_targets = True
                    refreshed_targets = await self._monitor.refresh_return_targets()
                    if refreshed_targets:
                        burst_targets = refreshed_targets
                        target_index = 0
                        self._emit(
                            (
                                "接口提示暂无可售/状态波动，已复检项目详情并切到 "
                                f"{burst_targets[0].desc}（sku_id={burst_targets[0].sku_id}）"
                            ),
                            "info",
                        )
                        continue
                if burst_decision.finished:
                    self._record_burst_finish(burst_decision)
                    next_burst_pause = self._burst_pause_seconds(burst_decision, result)
                    break
                if sold_out_result and self.config.return_monitor_enabled:
                    if self.status.attempts < self.config.max_attempts:
                        await self._sleep_continue_burst()
                    continue
                if self.status.attempts < self.config.max_attempts:
                    await self._sleep_before_retry(result)

        self.status.finished_reason = "达到最大尝试次数"
        self._clear_retry_status()
        self._emit("已达到最大尝试次数，停止", "warn")

    async def _run(self) -> None:
        self._reset_run_state(running=True)
        self._recorder.start(self.status, TELEMETRY_DIR)
        self._emit_status()
        try:
            self._client = BiliClient(self.config.cookie, http2_enabled=self.config.http2_enabled)
            self.status.transport = getattr(self._client, "transport", "")
            if not self._client.is_logged_in:
                self._emit("Cookie 不完整或未登录，无法抢票", "error")
                self.status.finished_reason = "未登录"
                return
            buyers, primary_target, deadline = await self._prepare_run()
            await self._run_order_loop(buyers, primary_target, deadline)
        except asyncio.CancelledError:
            self._emit("已手动停止", "warn")
            raise
        except Exception as exc:  # noqa: BLE001
            self._emit(f"运行异常: {exc}", "error")
            self.status.finished_reason = str(exc)
        finally:
            self.status.running = False
            self._set_phase(GrabPhase.FINISHED)
            self._recorder.record(self.status, "run_finish", outcome=self.status.finished_reason or "finished")
            self._emit_status()
            if self._client:
                await self._client.aclose()
                self._client = None

    async def _handle_risk(self, v_voucher: str, extra_params: dict[str, Any]) -> bool:
        if not v_voucher:
            self._emit("触发风控但未获得 v_voucher，稍后重试", "warn")
            return False
        self._emit("触发风控，开始人机验证…", "warn")
        self._set_phase(GrabPhase.CAPTCHA)
        self.status.waiting_captcha = True
        self._emit_status()
        try:
            assert self._client is not None
            grisk_id = await self.risk.handle(self._client, v_voucher)
            extra_params["gaia_vtoken"] = grisk_id
            self._emit("人机验证通过，恢复抢票", "success")
            return True
        except (RiskError, asyncio.TimeoutError) as exc:
            self._emit(f"风控处理失败: {exc}", "error")
            return False
        finally:
            self.status.waiting_captcha = False
            self._emit_status()

    async def _invoke_risk_handler(self, v_voucher: str, extra_params: dict[str, Any]) -> bool:
        """晚绑定到 ``_handle_risk``，让测试对该方法的 patch 依然生效。"""

        return await self._handle_risk(v_voucher, extra_params)

    # ---- 兼容转发 ----
    # 现有回归测试直接访问这些成员。逻辑已搬到独立模块，这里只做转发，
    # 待测试改到新单元后整块删除。
    async def _attempt_order(
        self,
        buyers: list[ticket.Buyer],
        target: TicketTarget,
        extra_params: dict[str, Any],
    ) -> AttemptResult:
        return await self._executor.attempt(buyers, target, extra_params)

    async def _handle_prepare_failure(
        self,
        prep: ticket.PrepareResult,
        attempt: int,
        extra_params: dict[str, Any],
    ) -> AttemptResult:
        return await self._executor.handle_prepare_failure(prep, attempt, extra_params)

    def _apply_corrected_pay_money(self, pay_money: int, target: TicketTarget | None = None) -> bool:
        return self._executor.apply_corrected_pay_money(pay_money, target)

    def _sku_looks_available(self, sku: ticket.TicketSku) -> bool:
        return grab_targets.sku_looks_available(self.config, sku)

    def _parse_local_time(self, value: str, label: str) -> datetime | None:
        return self._clock.parse(value, label)

    async def _wait_until_prewarm(self, target: datetime | None) -> None:
        await self._clock.wait_until_prewarm(target)

    async def _wait_until_start(self, target: datetime | None = None) -> None:
        await self._clock.wait_until_start(target)

    async def _wait_for_return_ticket(self, deadline: datetime) -> list[TicketTarget]:
        return await self._monitor.wait_for_return_ticket(deadline)

    def _available_monitor_targets(self, project: ticket.Project) -> list[TicketTarget]:
        return self._ctx.available_monitor_targets(project)

    @property
    def _dynamic_interval(self) -> float:
        return self._pacing.dynamic_interval

    @property
    def _rate_limit_recovery_remaining(self) -> int:
        return self._pacing.rate_limit_recovery_remaining

    def _on_rate_limit(self) -> None:
        self._pacing.on_rate_limit()

    def _on_retryable(self) -> None:
        self._pacing.on_retryable()

    def _retry_delay(self) -> float:
        return self._pacing.retry_delay()

    def _reusable_token(self, target: TicketTarget) -> str:
        return self._tokens.reusable(target)

    @property
    def _cached_token(self) -> str:
        return self._tokens.token

    @_cached_token.setter
    def _cached_token(self, value: str) -> None:
        self._tokens.token = value

    @property
    def _cached_ptoken(self) -> str:
        return self._tokens.ptoken

    @_cached_ptoken.setter
    def _cached_ptoken(self, value: str) -> None:
        self._tokens.ptoken = value

    @property
    def _cached_token_at(self) -> float:
        return self._tokens.stored_at

    @_cached_token_at.setter
    def _cached_token_at(self, value: float) -> None:
        self._tokens.stored_at = value

    @property
    def _cached_token_sku_id(self) -> int:
        return self._tokens.sku_id

    @_cached_token_sku_id.setter
    def _cached_token_sku_id(self, value: int) -> None:
        self._tokens.sku_id = value


# 保持原有导入入口。
_has_pending_order = has_pending_order


_PERMANENT_RESOLVE_KEYWORDS = (
    "未匹配",
    "不能重复",
    "必须与购买数量",
    "票价无效",
    "未发售",
)


def _is_permanent_resolve_error(exc: Exception) -> bool:
    message = str(exc)
    return any(keyword in message for keyword in _PERMANENT_RESOLVE_KEYWORDS)
