"""测试共用的假客户端、响应桩与基类。"""

from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app.bili import ticket
from app.config import AppConfig


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
