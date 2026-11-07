"""单账号抢票的冲刺次数与阶段决策。"""

from __future__ import annotations

from .bili.errors import ResultKind
from .grab_models import AttemptOutcome, AttemptResult, BurstDecision, BurstState, GrabPhase

_INITIAL_BURST_ATTEMPTS = 24
_INITIAL_BURST_RATE_LIMITS = 3
_INITIAL_BURST_SOLD_OUTS = 2
_RETURN_BURST_ATTEMPTS = 6
_RETURN_BURST_RATE_LIMITS = 2
_RETURN_BURST_SOLD_OUTS = 1


class BalancedStrategyController:
    """单账号自动平衡策略：首发先短冲刺，回流阶段少撞限流。"""

    def __init__(self) -> None:
        self.phase = GrabPhase.IDLE
        self.burst: BurstState | None = None

    def set_phase(self, phase: GrabPhase) -> GrabPhase:
        self.phase = phase
        return phase

    def start_burst(self, phase: GrabPhase, configured_return_attempts: int) -> None:
        self.phase = phase
        if phase is GrabPhase.RETURN_BURST:
            max_attempts = max(1, min(configured_return_attempts, _RETURN_BURST_ATTEMPTS))
            self.burst = BurstState(
                phase=phase,
                max_create_attempts=max_attempts,
                max_rate_limits=_RETURN_BURST_RATE_LIMITS,
                max_sold_outs=max_attempts,
            )
            return

        self.burst = BurstState(
            phase=phase,
            max_create_attempts=_INITIAL_BURST_ATTEMPTS,
            max_rate_limits=_INITIAL_BURST_RATE_LIMITS,
            max_sold_outs=_INITIAL_BURST_SOLD_OUTS,
        )

    def in_return_burst(self) -> bool:
        """当前是否处于回流票冲刺，用于决定是否刷新候选票档。"""

        return self.burst is not None and self.burst.phase is GrabPhase.RETURN_BURST

    def record_attempt(self, result: AttemptResult) -> BurstDecision:
        if self.burst is None:
            return BurstDecision()

        if result.endpoint == "create":
            self.burst.create_attempts += 1
        if result.kind is ResultKind.RATE_LIMIT:
            self.burst.rate_limits += 1
        if result.outcome is AttemptOutcome.SOLD_OUT or result.kind is ResultKind.SOLD_OUT:
            self.burst.sold_outs += 1

        reason = ""
        if self.burst.rate_limits >= self.burst.max_rate_limits:
            reason = "rate_limit_wall"
        elif self.burst.sold_outs >= self.burst.max_sold_outs:
            reason = "sold_out_wall"
        elif self.burst.create_attempts >= self.burst.max_create_attempts:
            reason = "attempt_wall"

        decision = BurstDecision(
            finished=bool(reason),
            reason=reason,
            burst_attempts=self.burst.create_attempts,
            burst_rate_limits=self.burst.rate_limits,
            burst_sold_outs=self.burst.sold_outs,
        )
        if reason:
            self.burst = None
        return decision
