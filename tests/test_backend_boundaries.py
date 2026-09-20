"""后端职责拆分边界的行为回归测试，不访问真实 Bilibili 接口。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import server
from app.bili import ticket
from app.bili.client import BiliClient
from app.config import ADMIN_TOKEN_ENV, AppConfig, ServerConfig
from app.config_service import validate_grab_config
from app.grabber import Grabber
from app.time_utils import parse_local_datetime


class ServerBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig(server=ServerConfig(admin_token="initial-admin-token"))
        self.enterContext(patch.object(server.state, "config", self.config))
        self.enterContext(patch.object(server.state, "grabber", None))
        self.enterContext(patch.dict("os.environ", {ADMIN_TOKEN_ENV: ""}))
        self.client = TestClient(server.app)

    def test_auth_uses_replaced_config_for_http_and_websocket(self) -> None:
        old_headers = {"X-Admin-Token": "initial-admin-token"}
        new_headers = {"Authorization": "Bearer replacement-admin-token", "Origin": "https://example.com"}
        self.assertEqual(self.client.get("/api/config", headers=old_headers).status_code, 200)
        self.assertEqual(self.client.get("/api/config", headers=new_headers).status_code, 403)

        server.state.config = AppConfig(server=ServerConfig(
            admin_token="replacement-admin-token", allowed_origins=["https://example.com"],
        ))
        self.assertEqual(self.client.get("/api/config", headers=old_headers).status_code, 401)
        self.assertEqual(self.client.get("/api/config", headers=new_headers).status_code, 200)
        with self.assertRaises(WebSocketDisconnect) as raised:
            with self.client.websocket_connect("/ws", headers=old_headers):
                pass
        self.assertEqual(raised.exception.code, 1008)
        with self.client.websocket_connect("/ws", headers=new_headers):
            pass

    def test_empty_report_matches_idle_grabber_and_running_report_fields(self) -> None:
        headers = {"X-Admin-Token": "initial-admin-token"}
        empty = self.client.get("/api/grab/report", headers=headers).json()
        grabber = Grabber(self.config)
        server.state.grabber = grabber
        idle = self.client.get("/api/grab/report", headers=headers).json()
        self.assertEqual(empty, idle)
        grabber.status.running = True
        grabber.status.run_id = "example-run"
        grabber.status.attempts = 3
        running = self.client.get("/api/grab/report", headers=headers).json()
        self.assertEqual(set(empty), set(running))
        self.assertTrue(running["running"])
        self.assertEqual(running["attempts"], 3)


class GrabberLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch("app.grabber.TELEMETRY_DIR", self.directory))
        self.config = AppConfig(
            cookie="SESSDATA=secret; bili_jct=csrf; DedeUserID=1",
            project_id=100, screen_id=200, sku_id=300, buyer_ids=[1],
            count=1, interval_ms=100, max_attempts=1,
            time_sync_enabled=False, connection_prewarm_enabled=False, payment_link_enabled=False,
        )
        self.client = Mock(spec=BiliClient)
        self.client.is_logged_in = True
        self.client.transport = "http1"
        self.client.gen_bili_ticket.return_value = "ticket"
        self.enterContext(patch("app.grabber.BiliClient", return_value=self.client))
        buyers = [ticket.Buyer(buyer_id=1, name="Alice", tel="15555550123", id_card="secret-id")]
        project = ticket.Project(project_id=100, name="show", screens=[
            ticket.Screen(screen_id=200, name="screen", skus=[ticket.TicketSku(300, "vip", 8800, "sale", 1)]),
        ])
        self.enterContext(patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)))
        self.enterContext(patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)))
        self.prepare = self.enterContext(patch(
            "app.grabber.ticket.prepare_order",
            new=AsyncMock(return_value=ticket.PrepareResult(code=0, message="", token="token")),
        ))
        self.create = self.enterContext(patch(
            "app.grabber.ticket.create_order",
            new=AsyncMock(return_value=ticket.CreateResult(code=0, message="ok", order_id="order-1")),
        ))
        self.notify = self.enterContext(patch("app.grabber.notify.send_all", new=AsyncMock()))
        self.events: list[dict] = []
        self.grabber = Grabber(self.config, on_event=self.events.append)

    async def test_record_write_failure_does_not_interrupt_order(self) -> None:
        # 覆盖目录创建失败及文件写入被拒绝的路径。
        for failure in ("parent_is_file", "open_denied"):
            with self.subTest(failure=failure):
                if failure == "parent_is_file":
                    blocked_directory = self.directory / "not-a-directory"
                    blocked_directory.write_text("file", encoding="utf-8")
                    failure_patch = patch("app.grabber.TELEMETRY_DIR", blocked_directory)
                else:
                    failure_patch = patch("app.grab_reporting.Path.open", side_effect=PermissionError("denied"))
                with failure_patch:
                    await self.grabber._run()
                self.assertTrue(self.grabber.status.success)
                self.assertEqual(self.grabber.status.order_id, "order-1")
                self.assertEqual(self.grabber.report()["effective_order_attempts"], 1)
                self.assertFalse(self.grabber.status.running)
        self.assertEqual(self.create.await_count, 2)
        self.assertEqual(self.notify.await_count, 2)
        self.assertEqual(self.client.aclose.await_count, 2)

    async def test_cancel_during_prepare_closes_client_and_finishes_report(self) -> None:
        entered_prepare = asyncio.Event()

        async def pending_prepare(*args, **kwargs):
            entered_prepare.set()
            await asyncio.Event().wait()

        self.prepare.side_effect = pending_prepare
        await self.grabber.start()
        try:
            await asyncio.wait_for(entered_prepare.wait(), timeout=2)
        finally:
            await self.grabber.stop()
        self.client.aclose.assert_awaited_once()
        self.create.assert_not_awaited()
        self.assertIsNone(self.grabber._client)
        self.assertFalse(self.grabber.is_running())
        self.assertFalse(self.grabber.status.running)
        self.assertEqual(self.grabber.report()["phase"], "finished")
        rows = [json.loads(line) for line in Path(self.grabber.status.telemetry_path).read_text().splitlines()]
        self.assertEqual(rows[-1]["event"], "run_finish")

    async def test_next_run_resets_counters_backoff_and_prepare_token(self) -> None:
        self.config.max_attempts = 2
        self.create.side_effect = [
            ticket.CreateResult(code=429, message="限流"),
            ticket.CreateResult(code=100001, message="拥堵"),
            ticket.CreateResult(code=0, message="ok", order_id="second-run-order"),
        ]
        self.prepare.side_effect = [
            ticket.PrepareResult(code=0, message="", token=value)
            for value in ("first-token", "cached-token", "fresh-token")
        ]
        with patch("app.grabber.asyncio.sleep", new=AsyncMock()):
            await self.grabber._run()
        first_report = self.grabber.report()
        first_path = Path(first_report["telemetry_path"])
        first_contents = first_path.read_text()
        self.assertEqual(first_report["rate_limit_count"], 1)
        self.assertEqual(first_report["congestion_count"], 1)
        self.events.clear()

        await self.grabber._run()
        initial_status = next(event for event in self.events if event["type"] == "status")
        self.assertEqual(initial_status["attempts"], 0)
        self.assertEqual(initial_status["dynamic_interval_ms"], 100)
        second_report = self.grabber.report()
        self.assertEqual(second_report["rate_limit_count"], 0)
        self.assertEqual(second_report["congestion_count"], 0)
        self.assertEqual(second_report["effective_order_attempts"], 1)
        self.assertEqual(second_report["order_id"], "second-run-order")
        self.assertNotEqual(first_report["run_id"], second_report["run_id"])
        self.assertEqual(first_path.read_text(), first_contents)
        self.assertEqual(self.prepare.await_count, 3)
        self.assertEqual(self.create.await_args.args[5], "fresh-token")
        self.assertEqual(self.client.aclose.await_count, 2)

    async def test_client_initialization_failure_finishes_run(self) -> None:
        with patch("app.grabber.BiliClient", side_effect=RuntimeError("client setup failed")):
            await self.grabber._run()
        self.assertFalse(self.grabber.status.running)
        self.assertEqual(self.grabber.status.phase, "finished")
        self.assertEqual(self.grabber.status.finished_reason, "client setup failed")
        self.create.assert_not_awaited()

    def test_shared_time_parser_preserves_caller_errors_and_timezones(self) -> None:
        self.assertIsNone(parse_local_datetime(""))
        for value in ("2030-01-02T03:04:05", "2030-01-02T03:04:05+08:00"):
            with self.subTest(value=value):
                parsed = parse_local_datetime(value)
                self.assertIsNotNone(parsed.tzinfo)
                self.assertEqual(parsed, self.grabber._clock.parse(value, "开抢时间"))
                self.assertEqual((parsed.hour, parsed.minute), (3, 4))
                if value.endswith("+08:00"):
                    self.assertEqual(parsed.utcoffset().total_seconds(), 8 * 60 * 60)
        self.config.start_time = "invalid"
        self.assertEqual(validate_grab_config(self.config), "开抢时间格式无效")
        with self.assertRaisesRegex(RuntimeError, "^开抢时间格式无效: invalid$"):
            self.grabber._clock.parse("invalid", "开抢时间")
