"""测试共用的假客户端、响应桩与基类。"""

from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app.bili import ticket
from app.config import AppConfig
from app.grab_clock import GrabClock
from app.grab_context import GrabContext
from app.grab_models import GrabberStatus
from app.grab_pacing import PacingController, PrepareTokenCache
from app.grab_reporting import RunRecorder


def make_context(
    config: AppConfig,
    *,
    status: GrabberStatus | None = None,
    client=None,
    emit=None,
) -> GrabContext:
    """装配一个不依赖 Grabber 的运行上下文，供监控与下单的单元测试使用。"""

    status = GrabberStatus() if status is None else status
    emit = emit or (lambda *args, **kwargs: None)

    def set_phase(phase) -> None:
        status.phase = phase

    return GrabContext(
        config=config,
        status=status,
        recorder=RunRecorder(),
        pacing=PacingController(config, status),
        tokens=PrepareTokenCache(),
        clock=GrabClock(config, emit, set_phase),
        emit=emit,
        emit_status=lambda: None,
        set_phase=set_phase,
        client=client,
    )


def load_entrypoint_module():
    spec = importlib.util.spec_from_file_location(
        "ticket_buy_entrypoint_for_test",
        Path(__file__).resolve().parents[1] / "main.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeBiliClient:
    is_logged_in = True
    transport = "http1"

    def __init__(self, cookie: str, **kwargs) -> None:
        self.cookie = cookie
        self.kwargs = kwargs

    async def sync_server_time(self) -> int:
        return 0

    async def prewarm_connection(self) -> bool:
        return True

    async def gen_bili_ticket(self) -> str:
        return "ticket"

    async def aclose(self) -> None:
        return None


def ok_prepare(token: str = "token", ptoken: str = "") -> ticket.PrepareResult:
    return ticket.PrepareResult(code=0, message="", token=token, ptoken=ptoken)


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict | None = None,
        raise_json: bool = False,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self._raise_json = raise_json
        self.headers = headers or {}

    def json(self) -> dict:
        if self._raise_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class GrabberTestCase(unittest.IsolatedAsyncioTestCase):
    """抢票用例共用的配置与时间构造。"""

    def make_config(self) -> AppConfig:
        return AppConfig(
            cookie="SESSDATA=secret; bili_jct=csrf; DedeUserID=1",
            project_id=100,
            screen_id=200,
            sku_id=300,
            buyer_ids=[1],
            count=1,
            interval_ms=100,
            max_attempts=2,
        )

    def future_time(self) -> str:
        return (datetime.now().astimezone() + timedelta(minutes=5)).isoformat(timespec="seconds")
