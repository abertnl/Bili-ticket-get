"""单次下单尝试：预下单、创建订单、按错误码决定下一步动作。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import time
from typing import Any

from . import notify
from .bili import ticket
from .bili.errors import ResultKind, classify, describe
from .grab_context import GrabContext
from .grab_models import AttemptOutcome, AttemptResult, GrabPhase, TicketTarget

# 风控处理需要 Grabber 持有的验证码 solver，由外部注入。
RiskHandlerCallback = Callable[[str, dict[str, Any]], Awaitable[bool]]


def has_pending_order(message: str, code: int | None = None) -> bool:
    """会员购在已锁单/待支付时可能只返回提示文案，而不是标准成功码。"""

    return code in {100003, 100048, 100079} or "尚未完成订单" in message or "待支付" in message


class OrderExecutor:
    """执行下单尝试并把结果翻译成状态机动作。"""

    def __init__(self, ctx: GrabContext, handle_risk: RiskHandlerCallback) -> None:
        self._ctx = ctx
        self._handle_risk = handle_risk
        self._attempt_ms_total = 0

    # ---- 结果构造 ----
    def _retry_result(
        self,
        reason: str,
        delay: float,
        *,
        code: int | None = None,
        kind: ResultKind | None = None,
        endpoint: str = "",
    ) -> AttemptResult:
        self._ctx.status.retry_reason = reason
        self._ctx.status.retry_delay_ms = int(round(delay * 1000))
        return AttemptResult(
            AttemptOutcome.RETRY,
            retry_delay=delay,
            retry_reason=reason,
            code=code,
            kind=kind,
            endpoint=endpoint,
        )

    def _outcome_result(
        self,
        outcome: AttemptOutcome,
        *,
        code: int | None = None,
        kind: ResultKind | None = None,
        endpoint: str = "",
    ) -> AttemptResult:
        if outcome is not AttemptOutcome.RETRY:
            self._ctx.clear_retry_status()
        return AttemptResult(outcome, code=code, kind=kind, endpoint=endpoint)

    # ---- 计时 ----
    def elapsed_ms(self, started_at: float) -> int:
        return max(0, int(round((time.perf_counter() - started_at) * 1000)))

    def _record_attempt_timing(self, started_at: float) -> None:
        status = self._ctx.status
        elapsed = self.elapsed_ms(started_at)
        status.last_attempt_ms = elapsed
        self._attempt_ms_total += elapsed
        if status.attempts > 0:
            status.avg_attempt_ms = int(round(self._attempt_ms_total / status.attempts))

    # ---- 主流程 ----
    async def attempt(
        self,
        buyers: list[ticket.Buyer],
        target: TicketTarget,
        extra_params: dict[str, Any],
    ) -> AttemptResult:
        ctx = self._ctx
        config, status = ctx.config, ctx.status
        assert ctx.client is not None
        ctx.set_active_target(target)
        status.attempts += 1
        attempt = status.attempts
        attempt_started_at = time.perf_counter()
        status.last_prepare_ms = 0
        status.last_create_ms = 0
        status.retry_reason = ""
        status.retry_delay_ms = 0
        status.effective_delay_ms = 0

        token = ctx.tokens.reusable(target)
        ptoken = ""
        if token:
            ptoken = ctx.tokens.ptoken
            ctx.emit(f"[{attempt}] 复用 {target.desc} prepare token，跳过预下单", "info")
        else:
            try:
                status.last_endpoint = "prepare"
                prepare_started_at = time.perf_counter()
                prep = await ticket.prepare_order(
                    ctx.client,
                    config.project_id,
                    target.screen_id,
                    target.sku_id,
                    config.count,
                    buyers,
                )
                status.last_prepare_ms = self.elapsed_ms(prepare_started_at)
                ctx.recorder.record(
                    status,
                    "prepare_result",
                    endpoint="prepare",
                    attempt=attempt,
                    code=prep.code,
                    kind=classify(prep.code).value,
                    elapsed_ms=status.last_prepare_ms,
                    outcome="token" if prep.token else "no_token",
                )
            except Exception as exc:  # noqa: BLE001 网络抖动等
                status.last_prepare_ms = self.elapsed_ms(prepare_started_at)
                ctx.tokens.clear()
                status.network_errors += 1
                ctx.pacing.on_network_error()
                self._record_attempt_timing(attempt_started_at)
                status.last_endpoint = "prepare"
                ctx.recorder.record(
                    status,
                    "attempt_result",
                    endpoint="prepare",
                    attempt=attempt,
                    elapsed_ms=status.last_prepare_ms,
                    outcome="network_error",
                )
                ctx.emit(f"[{attempt}] prepare 异常: {exc}", "warn")
                ctx.emit_status()
                return self._retry_result(
                    "prepare 网络异常",
                    ctx.pacing.network_retry_delay(),
                    endpoint="prepare",
                )

            if not prep.token:
                ctx.tokens.clear()
                ctx.pacing.on_network_ok()
                self._record_attempt_timing(attempt_started_at)
                return await self.handle_prepare_failure(prep, attempt, extra_params)

            token = prep.token
            ptoken = prep.ptoken
            ctx.tokens.store(target.sku_id, token, ptoken)

        try:
            status.last_endpoint = "create"
            status.effective_order_attempts += 1
            create_started_at = time.perf_counter()
            result = await ticket.create_order(
                ctx.client,
                config.project_id,
                target.screen_id,
                target.sku_id,
                config.count,
                token,
                buyers,
                target.price,
                extra_params=extra_params or None,
                ptoken=ptoken,
                contact_name=config.contact_name,
                contact_tel=config.contact_tel,
            )
            status.last_create_ms = self.elapsed_ms(create_started_at)
            ctx.pacing.on_network_ok()
        except Exception as exc:  # noqa: BLE001 网络抖动等
            status.last_create_ms = self.elapsed_ms(create_started_at)
            ctx.tokens.clear()
            status.network_errors += 1
            ctx.pacing.on_network_error()
            self._record_attempt_timing(attempt_started_at)
            status.last_endpoint = "create"
            ctx.recorder.record(
                status,
                "attempt_result",
                endpoint="create",
                attempt=attempt,
                elapsed_ms=status.last_create_ms,
                outcome="network_error",
            )
            ctx.emit(f"[{attempt}] create 异常: {exc}", "warn")
            ctx.emit_status()
            return self._retry_result(
                "create 网络异常",
                ctx.pacing.network_retry_delay(),
                endpoint="create",
            )

        self._record_attempt_timing(attempt_started_at)

        # 默认让 token 失效；仅在拥堵可重试时短时复用（见末尾分支）
        ctx.tokens.clear()

        status.last_code = result.code
        status.last_message = result.message or describe(result.code)
        kind = classify(result.code)
        ctx.recorder.record(
            status,
            "attempt_result",
            endpoint="create",
            attempt=attempt,
            code=result.code,
            kind=kind.value,
            elapsed_ms=status.last_attempt_ms,
            outcome=result.message or describe(result.code),
        )
        ctx.emit(
            f"[{attempt}] code={result.code} {status.last_message}",
            "info",
            code=result.code,
        )
        ctx.emit_status()

        if kind is ResultKind.SUCCESS or has_pending_order(result.message, result.code):
            status.success = True
            status.order_id = result.order_id
            status.finished_reason = "订单已生成"
            success_message = result.message or "订单已生成，请在 10 分钟内完成支付"
            ctx.emit(
                f"{success_message} 请尽快前往 B 站完成支付",
                "success",
                order_id=result.order_id,
            )
            await self._prepare_payment_info(result.order_id)
            await self._notify_payment_required(
                result.order_id,
                success_message,
                payment_url=status.payment_url,
            )
            return self._outcome_result(
                AttemptOutcome.STOP,
                code=result.code,
                kind=kind,
                endpoint="create",
            )

        if kind is ResultKind.FATAL:
            status.finished_reason = status.last_message
            ctx.emit(f"致命错误，停止抢票: {status.last_message}", "error")
            return self._outcome_result(
                AttemptOutcome.STOP,
                code=result.code,
                kind=kind,
                endpoint="create",
            )

        if kind is ResultKind.RISK:
            ok = await self._handle_risk(result.v_voucher, extra_params)
            if ok:
                return self._retry_result(
                    "风控验证通过",
                    ctx.pacing.interval_seconds(),
                    code=result.code,
                    kind=kind,
                    endpoint="create",
                )
            return self._retry_result(
                "风控处理失败",
                ctx.pacing.rate_limit_retry_delay(),
                code=result.code,
                kind=kind,
                endpoint="create",
            )

        if result.code == 100051:
            return self._retry_result(
                "prepare token 过期，重新预下单",
                ctx.pacing.interval_seconds(),
                code=result.code,
                kind=kind,
                endpoint="create",
            )

        if result.code == 100034 and self.apply_corrected_pay_money(result.pay_money, target):
            return self._retry_result(
                "票价已刷新，重新预下单",
                ctx.pacing.interval_seconds(),
                code=result.code,
                kind=kind,
                endpoint="create",
            )

        if config.return_monitor_enabled and kind is ResultKind.SOLD_OUT:
            status.sold_out_count += 1
            ctx.emit("下单时库存不足", "warn")
            return self._outcome_result(
                AttemptOutcome.SOLD_OUT,
                code=result.code,
                kind=kind,
                endpoint="create",
            )

        if kind is ResultKind.SOLD_OUT:
            status.sold_out_count += 1
            return self._retry_result(
                "库存不足/已售罄",
                ctx.pacing.retry_delay(),
                code=result.code,
                kind=kind,
                endpoint="create",
            )

        if kind is ResultKind.RATE_LIMIT:
            ctx.set_phase(GrabPhase.RATE_LIMIT_COOLDOWN)
            return self._retry_result(
                "请求过频/风控拦截",
                ctx.pacing.rate_limit_retry_delay(),
                code=result.code,
                kind=kind,
                endpoint="create",
            )

        status.congestion_count += 1
        ctx.set_phase(GrabPhase.CONGESTION_RETRY)
        ctx.pacing.on_retryable()
        ctx.tokens.store(target.sku_id, token, ptoken)  # 拥堵可重试，短时复用该 token
        return self._retry_result(
            "接口返回可重试",
            ctx.pacing.retry_delay(),
            code=result.code,
            kind=kind,
            endpoint="create",
        )

    async def handle_prepare_failure(
        self,
        prep: ticket.PrepareResult,
        attempt: int,
        extra_params: dict[str, Any],
    ) -> AttemptResult:
        """prepare 未拿到 token 时，按错误码决定停止/风控/回流监控/重试。"""

        ctx = self._ctx
        config, status = ctx.config, ctx.status
        code = prep.code
        message = prep.message or describe(code)
        status.last_code = code
        status.last_message = message
        ctx.emit(f"[{attempt}] prepare 失败: {message}", "warn", code=code)
        ctx.emit_status()

        kind = classify(code)
        if has_pending_order(message, code):
            status.success = True
            status.finished_reason = "订单已生成"
            ctx.emit(
                f"{message} 请尽快前往 B 站完成支付",
                "success",
            )
            await self._notify_payment_required("", message)
            return self._outcome_result(
                AttemptOutcome.STOP,
                code=code,
                kind=kind,
                endpoint="prepare",
            )

        if kind is ResultKind.FATAL:
            status.finished_reason = message
            ctx.emit(f"致命错误，停止抢票: {message}", "error")
            return self._outcome_result(
                AttemptOutcome.STOP,
                code=code,
                kind=kind,
                endpoint="prepare",
            )

        if kind is ResultKind.RISK:
            ok = await self._handle_risk(prep.v_voucher, extra_params)
            if ok:
                return self._retry_result(
                    "风控验证通过",
                    ctx.pacing.interval_seconds(),
                    code=code,
                    kind=kind,
                    endpoint="prepare",
                )
            return self._retry_result(
                "风控处理失败",
                ctx.pacing.rate_limit_retry_delay(),
                code=code,
                kind=kind,
                endpoint="prepare",
            )

        if kind is ResultKind.SOLD_OUT:
            status.sold_out_count += 1
            if config.return_monitor_enabled:
                ctx.emit("预下单时库存不足", "warn")
                return self._outcome_result(
                    AttemptOutcome.SOLD_OUT,
                    code=code,
                    kind=kind,
                    endpoint="prepare",
                )
            return self._retry_result(
                "库存不足/已售罄",
                ctx.pacing.interval_seconds(),
                code=code,
                kind=kind,
                endpoint="prepare",
            )

        if kind is ResultKind.RATE_LIMIT:
            ctx.set_phase(GrabPhase.RATE_LIMIT_COOLDOWN)
            return self._retry_result(
                "请求过频/风控拦截",
                ctx.pacing.rate_limit_retry_delay(),
                code=code,
                kind=kind,
                endpoint="prepare",
            )

        status.congestion_count += 1
        ctx.set_phase(GrabPhase.CONGESTION_RETRY)
        ctx.pacing.on_retryable()
        return self._retry_result(
            "接口返回可重试",
            ctx.pacing.retry_delay(),
            code=code,
            kind=kind,
            endpoint="prepare",
        )

    # ---- 票价校正 ----
    def apply_corrected_pay_money(self, pay_money: int, target: TicketTarget | None = None) -> bool:
        ctx = self._ctx
        if pay_money <= 0:
            return False
        if ctx.config.count > 1 and pay_money % ctx.config.count == 0:
            pay_money = pay_money // ctx.config.count
        current_price = target.price if target is not None else ctx.current_pay_money
        if pay_money <= 0 or pay_money == current_price:
            return False
        if target is not None:
            target.price = pay_money
        ctx.current_pay_money = pay_money
        target_label = f"{target.desc} " if target is not None else ""
        ctx.emit(f"{target_label}票价已按接口提示刷新为 {pay_money / 100:.2f} 元", "warn")
        return True

    # ---- 抢中后的支付信息与推送 ----
    async def _prepare_payment_info(self, order_id: str) -> None:
        ctx = self._ctx
        if not ctx.config.payment_link_enabled or not order_id:
            return
        ctx.status.payment_url = ticket.get_order_detail_url(order_id)
        try:
            assert ctx.client is not None
            ctx.status.pay_qrcode_url = await ticket.get_pay_qrcode_url(ctx.client, order_id)
        except Exception as exc:  # noqa: BLE001
            ctx.emit(f"支付二维码获取失败: {exc}", "warn")
        ctx.recorder.record(
            ctx.status,
            "payment_info",
            endpoint="payment",
            attempt=ctx.status.attempts,
            code=ctx.status.last_code,
            outcome="ready" if ctx.status.payment_url else "unavailable",
        )
        ctx.emit_status()

    async def _notify_payment_required(
        self,
        order_id: str,
        message: str = "订单已生成",
        *,
        payment_url: str = "",
    ) -> None:
        ctx = self._ctx
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
                ctx.config.notify,
                title=title,
                body=body,
            )
        except Exception as exc:  # noqa: BLE001
            ctx.emit(f"推送失败: {exc}", "warn")
