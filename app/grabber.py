"""抢票状态机：定时开抢、请求间隔、重试、风控接入、抢中停止。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import json
from pathlib import Path
import re
import time
from typing import Any
from uuid import uuid4

from . import notify
from .bili import ticket
from .bili.captcha import ManualSolver, build_solver
from .bili.client import BiliClient
from .bili.errors import ResultKind, classify, describe
from .bili.risk import RiskError, RiskHandler
from .config import AppConfig

# 日志事件回调：接收一个 dict 事件（type/level/message/...）
EventCallback = Callable[[dict[str, Any]], None]

# prepare token 最长复用时长（秒）：仅在拥堵可重试时短时复用，减半请求量
_TOKEN_REUSE_SECONDS = 2.5
# 连续触发限流后进入更长冷却。短间隔会把同一账号/IP持续压在 429 窗口里。
_RATE_LIMIT_COOLDOWN_AFTER = 3
_RATE_LIMIT_COOLDOWN_MAX_SECONDS = 30.0
_INITIAL_SOLD_OUT_BURST_ATTEMPTS = 3
_RETURN_SOLD_OUT_BURST_ATTEMPTS = 3
TELEMETRY_DIR = Path("runtime/grab-runs")


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


class BalancedStrategyController:
    """单账号自动平衡策略：首发先短冲刺，回流阶段少撞限流。"""

    def __init__(self) -> None:
        self.phase = GrabPhase.IDLE

    def set_phase(self, phase: GrabPhase) -> GrabPhase:
        self.phase = phase
        return phase

    def initial_sold_out_limit(self) -> int:
        return _INITIAL_SOLD_OUT_BURST_ATTEMPTS

    def return_sold_out_limit(self, configured_limit: int) -> int:
        return max(1, min(configured_limit, _RETURN_SOLD_OUT_BURST_ATTEMPTS))


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
    phase: str = GrabPhase.IDLE.value
    run_id: str = ""
    telemetry_path: str = ""
    effective_delay_ms: int = 0
    last_endpoint: str = ""
    effective_order_attempts: int = 0
    time_offset_ms: int = 0
    prewarm_ok: bool = False
    transport: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class Grabber:
    """单账号抢票任务。"""

    def __init__(self, config: AppConfig, on_event: EventCallback | None = None) -> None:
        self.config = config
        self.on_event = on_event or (lambda e: None)
        self.status = GrabberStatus()
        self.solver = build_solver(config.captcha_mode, config.rrocr_token)
        self.risk = RiskHandler(self.solver)
        self.strategy = BalancedStrategyController()
        self._task: asyncio.Task | None = None
        self._client: BiliClient | None = None
        self._attempt_ms_total = 0
        self._consecutive_network_errors = 0
        self._dynamic_interval = self._interval_seconds()
        self._cached_token = ""
        self._cached_ptoken = ""
        self._cached_token_at = 0.0
        self._telemetry_path: Path | None = None
        self._run_started_at = 0.0
        self._time_offset_seconds = 0.0
        self._current_pay_money = 0

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
        self.status.phase = self.strategy.set_phase(phase).value

    def _start_telemetry(self) -> None:
        run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
        self.status.run_id = run_id
        self._telemetry_path = TELEMETRY_DIR / f"{run_id}.jsonl"
        self.status.telemetry_path = str(self._telemetry_path)
        self._run_started_at = time.perf_counter()
        self._record_telemetry("run_start", outcome="running")

    def _record_telemetry(
        self,
        event: str,
        *,
        endpoint: str = "",
        attempt: int | None = None,
        code: int | None = None,
        kind: str = "",
        delay_ms: int | None = None,
        elapsed_ms: int | None = None,
        stock_status: str = "",
        outcome: str = "",
    ) -> None:
        if not self._telemetry_path:
            return
        payload: dict[str, Any] = {
            "run_id": self.status.run_id,
            "time": datetime.now().isoformat(timespec="milliseconds"),
            "event": event,
            "phase": self.status.phase,
            "endpoint": endpoint,
            "attempt": attempt,
            "code": code,
            "kind": kind,
            "delay_ms": delay_ms,
            "elapsed_ms": elapsed_ms,
            "dynamic_interval_ms": self.status.dynamic_interval_ms,
            "consecutive_rate_limits": self.status.consecutive_rate_limits,
            "stock_status": stock_status,
            "outcome": outcome,
            "time_offset_ms": self.status.time_offset_ms,
            "prewarm_ok": self.status.prewarm_ok,
            "transport": self.status.transport,
            "payment_url_present": bool(self.status.payment_url),
        }
        try:
            self._telemetry_path.parent.mkdir(parents=True, exist_ok=True)
            with self._telemetry_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            # 抢票流程优先，复盘文件写入失败不应中断下单。
            return

    def report(self) -> dict[str, Any]:
        elapsed_ms = 0
        if self._run_started_at:
            elapsed_ms = int(round((time.perf_counter() - self._run_started_at) * 1000))
        return {
            "run_id": self.status.run_id,
            "phase": self.status.phase,
            "running": self.status.running,
            "success": self.status.success,
            "finished_reason": self.status.finished_reason,
            "attempts": self.status.attempts,
            "monitor_checks": self.status.monitor_checks,
            "effective_order_attempts": self.status.effective_order_attempts,
            "rate_limit_count": self.status.rate_limit_count,
            "congestion_count": self.status.congestion_count,
            "sold_out_count": self.status.sold_out_count,
            "network_errors": self.status.network_errors,
            "telemetry_path": self.status.telemetry_path,
            "order_id": self.status.order_id,
            "payment_url": self.status.payment_url,
            "pay_qrcode_url": self.status.pay_qrcode_url,
            "time_offset_ms": self.status.time_offset_ms,
            "prewarm_ok": self.status.prewarm_ok,
            "transport": self.status.transport,
            "elapsed_ms": elapsed_ms,
        }

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
    async def _wait_until_prewarm(self, target: datetime | None) -> None:
        if target is None:
            return
        self._set_phase(GrabPhase.PREWARM)
        prewarm_at = target - timedelta(seconds=self.config.prewarm_seconds)
        while True:
            now = self._now_for(target)
            remaining_to_prewarm = (prewarm_at - now).total_seconds()
            if remaining_to_prewarm <= 0:
                return
            remaining_to_start = (target - now).total_seconds()
            self._emit(f"距开抢还有 {remaining_to_start:.1f}s，等待预热", "info")
            await asyncio.sleep(min(remaining_to_prewarm, self._coarse_wait_step(remaining_to_start)))

    async def _wait_until_start(self, target: datetime | None = None) -> None:
        if target is None:
            target = self._parse_local_time(self.config.start_time, "开抢时间")
        if target is None:
            return
        self._set_phase(GrabPhase.START_WAIT)
        initial_remaining = (target - self._now_for(target)).total_seconds()
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

    def _coarse_wait_step(self, remaining_to_start: float) -> float:
        if remaining_to_start > 300:
            return 60.0
        if remaining_to_start > 60:
            return 10.0
        if remaining_to_start > 10:
            return 2.0
        return 1.0

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
        return self._find_target_sku(project).price

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

    def _parse_local_time(self, value: str, label: str) -> datetime | None:
        if not value:
            return None
        try:
            target = datetime.fromisoformat(value)
        except ValueError as exc:
            raise RuntimeError(f"{label}格式无效: {value}") from exc
        if target.tzinfo is None:
            target = target.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return target

    def _now_for(self, target: datetime) -> datetime:
        return datetime.now(tz=target.tzinfo) + timedelta(seconds=self._time_offset_seconds)

    def _interval_seconds(self) -> float:
        return self.config.interval_ms / 1000.0

    def _network_retry_delay(self) -> float:
        base = max(self._interval_seconds(), 0.2)
        multiplier = min(2 ** max(self._consecutive_network_errors - 1, 0), 8)
        return min(base * multiplier, self.config.network_backoff_max_ms / 1000.0)

    # ---- AIMD 自适应限速 ----
    def _aimd_floor(self) -> float:
        return max(self._interval_seconds(), 0.05)

    def _aimd_ceiling(self) -> float:
        return max(self.config.max_interval_ms / 1000.0, self._aimd_floor())

    def _on_rate_limit(self) -> None:
        """收到限流信号（429/412）：乘性退避抬高动态间隔。"""

        self.status.rate_limit_count += 1
        self.status.consecutive_rate_limits += 1
        if self.config.adaptive_rate_enabled:
            self._dynamic_interval = min(self._dynamic_interval * 2.0, self._aimd_ceiling())
        self.status.dynamic_interval_ms = int(round(self._dynamic_interval * 1000))

    def _on_retryable(self) -> None:
        """收到可重试（拥堵）响应：加性下调动态间隔，逐步逼近最优频率。"""

        self.status.consecutive_rate_limits = 0
        if self.config.adaptive_rate_enabled:
            self._dynamic_interval = max(self._dynamic_interval - 0.05, self._aimd_floor())
        self.status.dynamic_interval_ms = int(round(self._dynamic_interval * 1000))

    def _retry_delay(self) -> float:
        if not self.config.adaptive_rate_enabled:
            return self._interval_seconds()
        return self._dynamic_interval

    def _reusable_token(self) -> str:
        """返回可复用的 prepare token（仅拥堵重试且未过期），否则空串。"""

        if not self._cached_token:
            return ""
        if time.perf_counter() - self._cached_token_at > _TOKEN_REUSE_SECONDS:
            self._cached_token = ""
            self._cached_ptoken = ""
            return ""
        return self._cached_token

    def _rate_limit_retry_delay(self) -> float:
        self._on_rate_limit()
        base = self._dynamic_interval if self.config.adaptive_rate_enabled else self._interval_seconds()
        delay = max(base, self.config.rate_limit_backoff_ms / 1000.0)
        if self.status.consecutive_rate_limits >= _RATE_LIMIT_COOLDOWN_AFTER:
            cooldown = self.config.rate_limit_cooldown_ms / 1000.0
            cooldown_round = self.status.consecutive_rate_limits - _RATE_LIMIT_COOLDOWN_AFTER
            delay = max(delay, min(cooldown * (2**cooldown_round), _RATE_LIMIT_COOLDOWN_MAX_SECONDS))
        return delay

    def _retry_result(self, reason: str, delay: float) -> AttemptResult:
        self.status.retry_reason = reason
        self.status.retry_delay_ms = int(round(delay * 1000))
        return AttemptResult(AttemptOutcome.RETRY, retry_delay=delay, retry_reason=reason)

    def _clear_retry_status(self) -> None:
        self.status.retry_reason = ""
        self.status.retry_delay_ms = 0

    def _outcome_result(self, outcome: AttemptOutcome) -> AttemptResult:
        if outcome is not AttemptOutcome.RETRY:
            self._clear_retry_status()
        return AttemptResult(outcome)

    def _elapsed_ms(self, started_at: float) -> int:
        return max(0, int(round((time.perf_counter() - started_at) * 1000)))

    def _record_attempt_timing(self, started_at: float) -> None:
        elapsed = self._elapsed_ms(started_at)
        self.status.last_attempt_ms = elapsed
        self._attempt_ms_total += elapsed
        if self.status.attempts > 0:
            self.status.avg_attempt_ms = int(round(self._attempt_ms_total / self.status.attempts))

    async def _sleep_before_retry(self, result: AttemptResult) -> None:
        if result.retry_delay <= 0:
            return
        self.status.effective_delay_ms = self.status.retry_delay_ms
        self._record_telemetry(
            "retry_sleep",
            endpoint=self.status.last_endpoint,
            attempt=self.status.attempts,
            code=self.status.last_code,
            delay_ms=self.status.retry_delay_ms,
            outcome=result.retry_reason,
        )
        self._emit(
            f"等待 {self.status.retry_delay_ms}ms 后重试（{result.retry_reason}）",
            "info",
            retry_delay_ms=self.status.retry_delay_ms,
            retry_reason=result.retry_reason,
        )
        self._emit_status()
        await asyncio.sleep(result.retry_delay)

    async def _sleep_continue_burst(self) -> None:
        """库存不足但尚未达到回监控的冲刺轮数时，给一个自适应间隔再继续冲刺。"""

        delay = self._retry_delay()
        if delay <= 0:
            return
        delay_ms = int(round(delay * 1000))
        self.status.effective_delay_ms = delay_ms
        self._record_telemetry(
            "retry_sleep",
            endpoint=self.status.last_endpoint,
            attempt=self.status.attempts,
            code=self.status.last_code,
            delay_ms=delay_ms,
            outcome="库存不足继续冲刺",
        )
        self._emit(f"库存不足，继续冲刺（{delay_ms}ms 后重试）", "info")
        await asyncio.sleep(delay)

    async def _wait_for_return_ticket(self, deadline: datetime) -> bool:
        assert self._client is not None
        self._set_phase(GrabPhase.SOLD_OUT_MONITOR)
        interval = self.config.monitor_interval_ms / 1000.0
        while True:
            remaining = (deadline - self._now_for(deadline)).total_seconds()
            if remaining <= 0:
                self.status.finished_reason = "达到监控截止时间"
                self._emit("已达到监控截止时间，停止", "warn")
                return False

            self.status.monitor_checks += 1
            self.status.last_endpoint = "project"
            try:
                project = await ticket.get_project(self._client, self.config.project_id)
                sku = self._find_target_sku(project)
            except Exception as exc:  # noqa: BLE001
                rate_limit_code = _rate_limit_code_from_exception(exc)
                delay = min(interval, remaining)
                if rate_limit_code is not None:
                    self._set_phase(GrabPhase.RATE_LIMIT_COOLDOWN)
                    self.status.last_code = rate_limit_code
                    self.status.last_message = f"HTTP {rate_limit_code}"
                    delay = min(max(interval, self._rate_limit_retry_delay()), remaining)
                    self.status.retry_reason = "监控请求过频/风控拦截"
                    self.status.retry_delay_ms = int(round(delay * 1000))
                self.status.effective_delay_ms = int(round(delay * 1000))
                self.status.last_stock_status = f"监控异常: {exc}"
                self._record_telemetry(
                    "monitor_check",
                    endpoint="project",
                    code=rate_limit_code,
                    delay_ms=self.status.effective_delay_ms,
                    stock_status=self.status.last_stock_status,
                    outcome="error",
                )
                self._emit(f"[监控 {self.status.monitor_checks}] 获取票档失败: {exc}", "warn")
                self._emit_status()
                await asyncio.sleep(delay)
                self._set_phase(GrabPhase.SOLD_OUT_MONITOR)
                continue

            if self.status.retry_reason == "监控请求过频/风控拦截":
                self._clear_retry_status()
            self.status.last_stock_status = self._format_stock_status(sku)
            self._record_telemetry(
                "monitor_check",
                endpoint="project",
                code=0,
                stock_status=self.status.last_stock_status,
                outcome="available" if self._sku_looks_available(sku) else "unavailable",
            )
            self._emit(
                f"[监控 {self.status.monitor_checks}] {self.status.last_stock_status}",
                "info",
            )
            self._emit_status()
            if self._sku_looks_available(sku):
                self._emit("发现目标票档疑似可售，进入下单冲刺", "success")
                return True

            await asyncio.sleep(min(interval, remaining))

    def _find_target_sku(self, project: ticket.Project) -> ticket.TicketSku:
        for screen in project.screens:
            if screen.screen_id != self.config.screen_id:
                continue
            for sku in screen.skus:
                if sku.sku_id == self.config.sku_id:
                    return sku
        raise RuntimeError("未匹配到指定场次/票档，请重新加载演出并选择票档")

    def _format_stock_status(self, sku: ticket.TicketSku) -> str:
        sale_flag = sku.sale_flag or "状态未知"
        return f"{sku.desc} {sale_flag} 库存提示 {sku.num}"

    def _sku_looks_available(self, sku: ticket.TicketSku) -> bool:
        text = sku.sale_flag.strip()
        unavailable_words = ("售罄", "缺货", "不可售", "未开售", "暂停售票", "停售", "无票")
        if any(word in text for word in unavailable_words):
            return False
        if sku.num >= self.config.count:
            return True
        if not text:
            return False
        available_words = ("购买", "选座", "预订", "可售", "销售中", "立即")
        return any(word in text for word in available_words)

    async def _run(self) -> None:
        self.status = GrabberStatus(running=True)
        self.strategy = BalancedStrategyController()
        self._attempt_ms_total = 0
        self._consecutive_network_errors = 0
        self._dynamic_interval = self._interval_seconds()
        self.status.dynamic_interval_ms = int(round(self._dynamic_interval * 1000))
        self._cached_token = ""
        self._cached_ptoken = ""
        self._cached_token_at = 0.0
        self._telemetry_path = None
        self._time_offset_seconds = 0.0
        self._current_pay_money = 0
        self._start_telemetry()
        self._emit_status()
        self._client = BiliClient(self.config.cookie, http2_enabled=self.config.http2_enabled)
        self.status.transport = getattr(self._client, "transport", "")
        try:
            if not self._client.is_logged_in:
                self._emit("Cookie 不完整或未登录，无法抢票", "error")
                self.status.finished_reason = "未登录"
                return

            start_target = self._parse_local_time(self.config.start_time, "开抢时间")
            if self.config.time_sync_enabled and hasattr(self._client, "sync_server_time"):
                try:
                    offset_ms = await self._client.sync_server_time()
                except Exception as exc:  # noqa: BLE001
                    offset_ms = 0
                    self._emit(f"服务端时间同步失败，将使用本机时间: {exc}", "warn")
                else:
                    self._emit(f"服务端时间偏移估算 {offset_ms}ms", "info")
                self.status.time_offset_ms = offset_ms
                self._time_offset_seconds = offset_ms / 1000.0
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
                deadline = self._parse_local_time(self.config.monitor_end_time, "监控截止时间")
                if deadline is None:
                    raise RuntimeError("启用回流票监控时必须设置监控截止时间")
                if self._now_for(deadline) >= deadline:
                    raise RuntimeError("监控截止时间已过期")

            buyers, pay_money = await self._resolve_prewarm_data()
            self._current_pay_money = pay_money
            self._emit(f"购票人 {len(buyers)} 人，票价 {pay_money / 100:.2f} 元", "info")

            await self._wait_until_start(start_target)
            self._set_phase(GrabPhase.START_BURST)
            self._emit("开始抢票", "info")
            if self.config.return_monitor_enabled:
                self._emit("开始回流票监控", "info")

            extra_params: dict[str, Any] = {}
            initial_burst_pending = self.config.return_monitor_enabled

            while self.status.attempts < self.config.max_attempts:
                if self.config.return_monitor_enabled:
                    if initial_burst_pending:
                        initial_burst_pending = False
                        self._set_phase(GrabPhase.START_BURST)
                        sold_out_burst_limit = self.strategy.initial_sold_out_limit()
                    else:
                        assert deadline is not None
                        should_burst = await self._wait_for_return_ticket(deadline)
                        if not should_burst:
                            return
                        self._set_phase(GrabPhase.RETURN_BURST)
                        sold_out_burst_limit = self.strategy.return_sold_out_limit(
                            self.config.sold_out_burst_attempts
                        )
                else:
                    self._set_phase(GrabPhase.START_BURST)
                    sold_out_burst_limit = self.config.sold_out_burst_attempts

                sold_out_in_burst = 0
                while self.status.attempts < self.config.max_attempts:
                    result = await self._attempt_order(buyers, pay_money, extra_params)
                    if result.outcome is AttemptOutcome.STOP:
                        return
                    if result.outcome is AttemptOutcome.SOLD_OUT and self.config.return_monitor_enabled:
                        sold_out_in_burst += 1
                        if sold_out_in_burst >= sold_out_burst_limit:
                            break
                        if self.status.attempts < self.config.max_attempts:
                            await self._sleep_continue_burst()
                        continue
                    sold_out_in_burst = 0
                    if self.status.attempts < self.config.max_attempts:
                        await self._sleep_before_retry(result)

            self.status.finished_reason = "达到最大尝试次数"
            self._clear_retry_status()
            self._emit("已达到最大尝试次数，停止", "warn")
        except asyncio.CancelledError:
            self._emit("已手动停止", "warn")
            raise
        except Exception as exc:  # noqa: BLE001
            self._emit(f"运行异常: {exc}", "error")
            self.status.finished_reason = str(exc)
        finally:
            self.status.running = False
            self._set_phase(GrabPhase.FINISHED)
            self._record_telemetry("run_finish", outcome=self.status.finished_reason or "finished")
            self._emit_status()
            if self._client:
                await self._client.aclose()
                self._client = None

    async def _attempt_order(
        self,
        buyers: list[ticket.Buyer],
        pay_money: int,
        extra_params: dict[str, Any],
    ) -> AttemptResult:
        assert self._client is not None
        self.status.attempts += 1
        attempt = self.status.attempts
        attempt_started_at = time.perf_counter()
        self.status.last_prepare_ms = 0
        self.status.last_create_ms = 0
        self.status.retry_reason = ""
        self.status.retry_delay_ms = 0
        self.status.effective_delay_ms = 0

        token = self._reusable_token()
        ptoken = ""
        if token:
            ptoken = self._cached_ptoken
            self._emit(f"[{attempt}] 复用 prepare token，跳过预下单", "info")
        else:
            try:
                self.status.last_endpoint = "prepare"
                prepare_started_at = time.perf_counter()
                prep = await ticket.prepare_order(
                    self._client,
                    self.config.project_id,
                    self.config.screen_id,
                    self.config.sku_id,
                    self.config.count,
                    buyers,
                )
                self.status.last_prepare_ms = self._elapsed_ms(prepare_started_at)
                self._record_telemetry(
                    "prepare_result",
                    endpoint="prepare",
                    attempt=attempt,
                    code=prep.code,
                    kind=classify(prep.code).value,
                    elapsed_ms=self.status.last_prepare_ms,
                    outcome="token" if prep.token else "no_token",
                )
            except Exception as exc:  # noqa: BLE001 网络抖动等
                self.status.last_prepare_ms = self._elapsed_ms(prepare_started_at)
                self._cached_token = ""
                self._cached_ptoken = ""
                self.status.network_errors += 1
                self._consecutive_network_errors += 1
                self._record_attempt_timing(attempt_started_at)
                self.status.last_endpoint = "prepare"
                self._record_telemetry(
                    "attempt_result",
                    endpoint="prepare",
                    attempt=attempt,
                    elapsed_ms=self.status.last_prepare_ms,
                    outcome="network_error",
                )
                self._emit(f"[{attempt}] prepare 异常: {exc}", "warn")
                self._emit_status()
                return self._retry_result("prepare 网络异常", self._network_retry_delay())

            if not prep.token:
                self._cached_token = ""
                self._cached_ptoken = ""
                self._consecutive_network_errors = 0
                self._record_attempt_timing(attempt_started_at)
                return await self._handle_prepare_failure(prep, attempt, extra_params)

            token = prep.token
            ptoken = prep.ptoken
            self._cached_token = token
            self._cached_ptoken = ptoken
            self._cached_token_at = time.perf_counter()

        try:
            self.status.last_endpoint = "create"
            self.status.effective_order_attempts += 1
            create_started_at = time.perf_counter()
            result = await ticket.create_order(
                self._client,
                self.config.project_id,
                self.config.screen_id,
                self.config.sku_id,
                self.config.count,
                token,
                buyers,
                self._current_pay_money or pay_money,
                extra_params=extra_params or None,
                ptoken=ptoken,
            )
            self.status.last_create_ms = self._elapsed_ms(create_started_at)
            self._consecutive_network_errors = 0
        except Exception as exc:  # noqa: BLE001 网络抖动等
            self.status.last_create_ms = self._elapsed_ms(create_started_at)
            self._cached_token = ""
            self._cached_ptoken = ""
            self.status.network_errors += 1
            self._consecutive_network_errors += 1
            self._record_attempt_timing(attempt_started_at)
            self.status.last_endpoint = "create"
            self._record_telemetry(
                "attempt_result",
                endpoint="create",
                attempt=attempt,
                elapsed_ms=self.status.last_create_ms,
                outcome="network_error",
            )
            self._emit(f"[{attempt}] create 异常: {exc}", "warn")
            self._emit_status()
            return self._retry_result("create 网络异常", self._network_retry_delay())

        self._record_attempt_timing(attempt_started_at)

        # 默认让 token 失效；仅在拥堵可重试时短时复用（见末尾分支）
        self._cached_token = ""
        self._cached_ptoken = ""

        self.status.last_code = result.code
        self.status.last_message = result.message or describe(result.code)
        kind = classify(result.code)
        self._record_telemetry(
            "attempt_result",
            endpoint="create",
            attempt=attempt,
            code=result.code,
            kind=kind.value,
            elapsed_ms=self.status.last_attempt_ms,
            outcome=result.message or describe(result.code),
        )
        self._emit(
            f"[{attempt}] code={result.code} {self.status.last_message}",
            "info",
            code=result.code,
        )
        self._emit_status()

        if kind is ResultKind.SUCCESS or _has_pending_order(result.message, result.code):
            self.status.success = True
            self.status.order_id = result.order_id
            self.status.finished_reason = "订单已生成"
            success_message = result.message or "订单已生成，请在 10 分钟内完成支付"
            self._emit(
                f"{success_message} 请尽快前往 B 站完成支付",
                "success",
                order_id=result.order_id,
            )
            await self._prepare_payment_info(result.order_id)
            await self._notify_payment_required(
                result.order_id,
                success_message,
                payment_url=self.status.payment_url,
            )
            return self._outcome_result(AttemptOutcome.STOP)

        if kind is ResultKind.FATAL:
            self.status.finished_reason = self.status.last_message
            self._emit(f"致命错误，停止抢票: {self.status.last_message}", "error")
            return self._outcome_result(AttemptOutcome.STOP)

        if kind is ResultKind.RISK:
            ok = await self._handle_risk(result.v_voucher, extra_params)
            if ok:
                return self._retry_result("风控验证通过", self._interval_seconds())
            return self._retry_result("风控处理失败", self._rate_limit_retry_delay())

        if result.code == 100051:
            return self._retry_result("prepare token 过期，重新预下单", self._interval_seconds())

        if result.code == 100034 and self._apply_corrected_pay_money(result.pay_money):
            return self._retry_result("票价已刷新，重新预下单", self._interval_seconds())

        if self.config.return_monitor_enabled and kind is ResultKind.SOLD_OUT:
            self.status.sold_out_count += 1
            self._emit("下单时库存不足", "warn")
            return self._outcome_result(AttemptOutcome.SOLD_OUT)

        if kind is ResultKind.SOLD_OUT:
            self.status.sold_out_count += 1
            return self._retry_result("库存不足/已售罄", self._retry_delay())

        if kind is ResultKind.RATE_LIMIT:
            self._set_phase(GrabPhase.RATE_LIMIT_COOLDOWN)
            return self._retry_result("请求过频/风控拦截", self._rate_limit_retry_delay())

        self.status.congestion_count += 1
        self._set_phase(GrabPhase.CONGESTION_RETRY)
        self._on_retryable()
        self._cached_token = token  # 拥堵可重试，短时复用该 token
        self._cached_ptoken = ptoken
        return self._retry_result("接口返回可重试", self._retry_delay())

    async def _handle_prepare_failure(
        self,
        prep: ticket.PrepareResult,
        attempt: int,
        extra_params: dict[str, Any],
    ) -> AttemptResult:
        """prepare 未拿到 token 时，按错误码决定停止/风控/回流监控/重试。"""

        code = prep.code
        message = prep.message or describe(code)
        self.status.last_code = code
        self.status.last_message = message
        self._emit(f"[{attempt}] prepare 失败: {message}", "warn", code=code)
        self._emit_status()

        kind = classify(code)
        if _has_pending_order(message, code):
            self.status.success = True
            self.status.finished_reason = "订单已生成"
            self._emit(
                f"{message} 请尽快前往 B 站完成支付",
                "success",
            )
            await self._notify_payment_required("", message)
            return self._outcome_result(AttemptOutcome.STOP)

        if kind is ResultKind.FATAL:
            self.status.finished_reason = message
            self._emit(f"致命错误，停止抢票: {message}", "error")
            return self._outcome_result(AttemptOutcome.STOP)

        if kind is ResultKind.RISK:
            ok = await self._handle_risk(prep.v_voucher, extra_params)
            if ok:
                return self._retry_result("风控验证通过", self._interval_seconds())
            return self._retry_result("风控处理失败", self._rate_limit_retry_delay())

        if kind is ResultKind.SOLD_OUT:
            self.status.sold_out_count += 1
            if self.config.return_monitor_enabled:
                self._emit("预下单时库存不足", "warn")
                return self._outcome_result(AttemptOutcome.SOLD_OUT)
            return self._retry_result("库存不足/已售罄", self._interval_seconds())

        if kind is ResultKind.RATE_LIMIT:
            self._set_phase(GrabPhase.RATE_LIMIT_COOLDOWN)
            return self._retry_result("请求过频/风控拦截", self._rate_limit_retry_delay())

        self.status.congestion_count += 1
        self._set_phase(GrabPhase.CONGESTION_RETRY)
        self._on_retryable()
        return self._retry_result("接口返回可重试", self._retry_delay())

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

    def _apply_corrected_pay_money(self, pay_money: int) -> bool:
        if pay_money <= 0:
            return False
        if self.config.count > 1 and pay_money % self.config.count == 0:
            pay_money = pay_money // self.config.count
        if pay_money <= 0 or pay_money == self._current_pay_money:
            return False
        self._current_pay_money = pay_money
        self._emit(f"票价已按接口提示刷新为 {pay_money / 100:.2f} 元", "warn")
        return True

    async def _prepare_payment_info(self, order_id: str) -> None:
        if not self.config.payment_link_enabled or not order_id:
            return
        self.status.payment_url = ticket.get_order_detail_url(order_id)
        try:
            assert self._client is not None
            self.status.pay_qrcode_url = await ticket.get_pay_qrcode_url(self._client, order_id)
        except Exception as exc:  # noqa: BLE001
            self._emit(f"支付二维码获取失败: {exc}", "warn")
        self._record_telemetry(
            "payment_info",
            endpoint="payment",
            attempt=self.status.attempts,
            code=self.status.last_code,
            outcome="ready" if self.status.payment_url else "unavailable",
        )
        self._emit_status()

    async def _notify_payment_required(
        self,
        order_id: str,
        message: str = "订单已生成",
        *,
        payment_url: str = "",
    ) -> None:
        try:
            title = "会员购订单待支付"
            body = (
                f"订单 {order_id} 已生成，请在 10 分钟内支付"
                if order_id
                else f"{message} 请尽快前往 B 站订单页支付"
            )
            if payment_url:
                body = f"{body}\n付款页：{payment_url}"
            await notify.send_all(
                self.config.notify,
                title=title,
                body=body,
            )
        except Exception as exc:  # noqa: BLE001
            self._emit(f"推送失败: {exc}", "warn")


def _has_pending_order(message: str, code: int | None = None) -> bool:
    """会员购在已锁单/待支付时可能只返回提示文案，而不是标准成功码。"""

    return code in {100003, 100048, 100079} or "尚未完成订单" in message or "待支付" in message


# 预热解析时这些错误属于配置/校验问题，重试也不会变好，应立即中止
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


def _rate_limit_code_from_exception(exc: Exception) -> int | None:
    match = re.search(r"\b(412|429)\b", str(exc))
    if not match:
        return None
    return int(match.group(1))
