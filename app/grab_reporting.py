"""运行复盘记录及统一的报告序列化。"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

from .grab_models import GrabberStatus


# 对外报告的字段白名单：GrabberStatus 里其余字段属于内部节奏控制，不下发。
# 新增状态字段时，只有在这里登记后才会出现在报告里。
_REPORT_FIELDS: tuple[str, ...] = (
    "run_id",
    "phase",
    "running",
    "success",
    "finished_reason",
    "attempts",
    "monitor_checks",
    "effective_order_attempts",
    "rate_limit_count",
    "congestion_count",
    "sold_out_count",
    "network_errors",
    "telemetry_path",
    "order_id",
    "payment_url",
    "pay_qrcode_url",
    "time_offset_ms",
    "prewarm_ok",
    "transport",
    "target_sku_id",
    "target_sku_desc",
    "monitor_target_count",
    "last_monitor_candidates",
)


def build_report(status: GrabberStatus | None = None, *, elapsed_ms: int = 0) -> dict[str, Any]:
    """无任务时也使用相同的字段和默认值。"""

    if status is None:
        status = GrabberStatus()
    report: dict[str, Any] = {name: getattr(status, name) for name in _REPORT_FIELDS}
    report["elapsed_ms"] = elapsed_ms
    return report


class RunRecorder:
    """仅写入允许的复盘字段；磁盘错误不影响抢票。"""

    def __init__(self) -> None:
        self.path: Path | None = None
        self.started_at: float | None = None

    def start(self, status: GrabberStatus, directory: Path) -> None:
        run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
        status.run_id = run_id
        self.path = directory / f"{run_id}.jsonl"
        status.telemetry_path = str(self.path)
        self.started_at = time.perf_counter()
        self.record(status, "run_start", outcome="running")

    def report(self, status: GrabberStatus) -> dict[str, Any]:
        elapsed_ms = 0
        if self.started_at is not None:
            elapsed_ms = int(round((time.perf_counter() - self.started_at) * 1000))
        return build_report(status, elapsed_ms=elapsed_ms)

    def record(
        self,
        status: GrabberStatus,
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
        burst_attempts: int | None = None,
        burst_rate_limits: int | None = None,
        burst_sold_outs: int | None = None,
    ) -> None:
        if not self.path:
            return
        payload: dict[str, Any] = {
            "run_id": status.run_id,
            "time": datetime.now().isoformat(timespec="milliseconds"),
            "event": event,
            "phase": status.phase,
            "endpoint": endpoint,
            "attempt": attempt,
            "code": code,
            "kind": kind,
            "delay_ms": delay_ms,
            "elapsed_ms": elapsed_ms,
            "dynamic_interval_ms": status.dynamic_interval_ms,
            "consecutive_rate_limits": status.consecutive_rate_limits,
            "stock_status": stock_status,
            "outcome": outcome,
            "time_offset_ms": status.time_offset_ms,
            "prewarm_ok": status.prewarm_ok,
            "transport": status.transport,
            "payment_url_present": bool(status.payment_url),
            "target_sku_id": status.target_sku_id or None,
            "target_sku_desc": status.target_sku_desc,
            "monitor_target_count": status.monitor_target_count,
            "monitor_candidates": status.last_monitor_candidates,
        }
        if burst_attempts is not None:
            payload["burst_attempts"] = burst_attempts
        if burst_rate_limits is not None:
            payload["burst_rate_limits"] = burst_rate_limits
        if burst_sold_outs is not None:
            payload["burst_sold_outs"] = burst_sold_outs
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            # 抢票流程优先，复盘文件写入失败不应中断下单。
            return
