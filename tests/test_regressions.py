from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import notify
from app import server
from app.bili import ticket
from app.bili.errors import ResultKind, classify
from app.config import AppConfig, NotifyConfig
from app.grabber import AttemptOutcome, Grabber, _has_pending_order


class ServerConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        server.state.config = AppConfig(
            cookie="SESSDATA=secret; bili_jct=csrf; DedeUserID=1",
            rrocr_token="rrocr-secret",
            notify=NotifyConfig(
                bark_url="https://api.day.app/bark-secret",
                serverchan_key="SCT-secret",
                imessage_recipient="+15555550123",
            ),
        )
        server.state.grabber = None
        self.client = TestClient(server.app)

    def test_config_response_redacts_sensitive_values(self) -> None:
        response = self.client.get("/api/config")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertNotIn("cookie", data)
        self.assertNotIn("rrocr_token", data)
        self.assertEqual(
            data["notify"],
            {"bark_url": "", "serverchan_key": "", "imessage_recipient": ""},
        )
        self.assertTrue(data["has_cookie"])
        self.assertTrue(data["has_rrocr_token"])
        self.assertEqual(
            data["notify_configured"],
            {"bark_url": True, "serverchan_key": True, "imessage_recipient": True},
        )

    def test_empty_sensitive_update_preserves_existing_values(self) -> None:
        with patch("app.server.save_config"):
            response = self.client.post(
                "/api/config",
                json={
                    "count": 2,
                    "rrocr_token": "",
                    "notify": {"bark_url": "", "serverchan_key": "", "imessage_recipient": ""},
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["config"]["count"], 2)
        self.assertEqual(server.state.config.rrocr_token, "rrocr-secret")
        self.assertEqual(server.state.config.notify.bark_url, "https://api.day.app/bark-secret")
        self.assertEqual(server.state.config.notify.serverchan_key, "SCT-secret")
        self.assertEqual(server.state.config.notify.imessage_recipient, "+15555550123")

    def test_config_rejects_invalid_values(self) -> None:
        cases = [
            {"count": 0},
            {"interval_ms": 99},
            {"max_attempts": 0},
            {"prewarm_seconds": -1},
            {"rate_limit_backoff_ms": 999},
            {"network_backoff_max_ms": 99},
            {"monitor_interval_ms": 999},
            {"captcha_mode": "bad"},
            {"buyer_ids": [1, 0]},
        ]

        for payload in cases:
            with self.subTest(payload=payload):
                response = self.client.post("/api/config", json=payload)
                self.assertEqual(response.status_code, 422)

    def test_project_id_must_be_positive(self) -> None:
        response = self.client.get("/api/project?project_id=0")

        self.assertEqual(response.status_code, 422)

    def test_external_route_errors_are_structured(self) -> None:
        with patch("app.server.auth.generate_qr", new=AsyncMock(side_effect=RuntimeError("offline"))):
            response = self.client.get("/api/login/qr")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["ok"], False)
        self.assertIn("offline", response.json()["message"])

    def test_grab_start_rejects_incomplete_local_config(self) -> None:
        response = self.client.post("/api/grab/start")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["ok"], False)
        self.assertIn("选择演出", response.json()["message"])
        self.assertIsNone(server.state.grabber)

    def test_grab_start_rejects_invalid_monitor_end_time(self) -> None:
        server.state.config = AppConfig(
            cookie="SESSDATA=secret; bili_jct=csrf; DedeUserID=1",
            project_id=100,
            screen_id=200,
            sku_id=300,
            buyer_ids=[1],
            return_monitor_enabled=True,
            monitor_end_time="bad-date",
        )

        response = self.client.post("/api/grab/start")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["ok"], False)
        self.assertIn("监控截止时间格式无效", response.json()["message"])
        self.assertIsNone(server.state.grabber)


class NotifyTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_all_includes_imessage_channel(self) -> None:
        config = NotifyConfig(imessage_recipient="+15555550123")

        with patch("app.notify._send_imessage", new=AsyncMock()) as send_imessage:
            await notify.send_all(config, "会员购订单待支付", "请及时支付")

        send_imessage.assert_awaited_once_with(
            "+15555550123",
            "会员购订单待支付",
            "请及时支付",
        )

    async def test_send_all_attempts_later_channels_after_failure(self) -> None:
        config = NotifyConfig(
            bark_url="https://api.day.app/bark-secret",
            imessage_recipient="+15555550123",
        )

        with (
            patch("app.notify._send_bark", new=AsyncMock(side_effect=RuntimeError("bark down"))),
            patch("app.notify._send_imessage", new=AsyncMock()) as send_imessage,
        ):
            with self.assertRaisesRegex(RuntimeError, "Bark: bark down"):
                await notify.send_all(config, "会员购订单待支付", "请及时支付")

        send_imessage.assert_awaited_once()

    async def test_send_imessage_invokes_osascript_with_message_text(self) -> None:
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("app.notify.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as create_proc:
            await notify._send_imessage("+15555550123", "会员购订单待支付", "请及时支付")

        create_proc.assert_awaited_once()
        args = create_proc.await_args.args
        self.assertEqual(args[0], "osascript")
        self.assertEqual(args[1], "-e")
        self.assertEqual(args[3], "+15555550123")
        self.assertEqual(args[4], "会员购订单待支付\n请及时支付")
        proc.communicate.assert_awaited_once()

    async def test_send_imessage_reports_osascript_failure(self) -> None:
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", "未授权".encode()))

        with patch("app.notify.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with self.assertRaisesRegex(RuntimeError, "未授权"):
                await notify._send_imessage("+15555550123", "会员购订单待支付", "请及时支付")


class ErrorClassificationTests(unittest.TestCase):
    def test_rate_limit_codes_are_distinct_from_regular_retry(self) -> None:
        self.assertIs(classify(429), ResultKind.RATE_LIMIT)
        self.assertIs(classify(412), ResultKind.RATE_LIMIT)
        self.assertIs(classify(100001), ResultKind.RETRY)


class FakeBiliClient:
    is_logged_in = True

    def __init__(self, cookie: str) -> None:
        self.cookie = cookie

    async def gen_bili_ticket(self) -> str:
        return "ticket"

    async def aclose(self) -> None:
        return None


def ok_prepare(token: str = "token") -> ticket.PrepareResult:
    return ticket.PrepareResult(code=0, message="", token=token)


class GrabberTests(unittest.IsolatedAsyncioTestCase):
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

    def test_has_pending_order_matches_known_payment_messages(self) -> None:
        self.assertTrue(_has_pending_order("你有尚未完成订单，请先支付"))
        self.assertTrue(_has_pending_order("存在待支付订单"))
        self.assertFalse(_has_pending_order("库存不足"))

    async def test_invalid_price_stops_before_prepare(self) -> None:
        events: list[dict] = []
        grabber = Grabber(self.make_config(), on_event=events.append)
        buyers = [ticket.Buyer(buyer_id=1, name="Alice", tel="1", id_card="id")]
        project = ticket.Project(
            project_id=100,
            name="show",
            screens=[
                ticket.Screen(
                    screen_id=200,
                    name="screen",
                    skus=[ticket.TicketSku(300, "vip", 0, "sale", 1)],
                )
            ],
        )

        with (
            patch("app.grabber.BiliClient", FakeBiliClient),
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
            patch("app.grabber.ticket.prepare_order", new=AsyncMock()) as prepare_order,
        ):
            await grabber._run()

        prepare_order.assert_not_awaited()
        self.assertFalse(grabber.status.running)
        self.assertIn("票价无效", grabber.status.finished_reason)

    async def test_prepare_fatal_error_stops_before_create(self) -> None:
        events: list[dict] = []
        grabber = Grabber(self.make_config(), on_event=events.append)
        buyers = [ticket.Buyer(buyer_id=1, name="Alice", tel="1", id_card="id")]
        project = ticket.Project(
            project_id=100,
            name="show",
            screens=[
                ticket.Screen(
                    screen_id=200,
                    name="screen",
                    skus=[ticket.TicketSku(300, "vip", 8800, "sale", 1)],
                )
            ],
        )

        with (
            patch("app.grabber.BiliClient", FakeBiliClient),
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
            patch(
                "app.grabber.ticket.prepare_order",
                new=AsyncMock(
                    return_value=ticket.PrepareResult(code=-101, message="账号未登录"),
                ),
            ),
            patch("app.grabber.ticket.create_order", new=AsyncMock()) as create_order,
        ):
            await grabber._run()

        create_order.assert_not_awaited()
        self.assertFalse(grabber.status.running)
        self.assertIn("账号未登录", grabber.status.finished_reason)

    async def test_prepare_risk_triggers_captcha_handling(self) -> None:
        events: list[dict] = []
        grabber = Grabber(self.make_config(), on_event=events.append)
        buyers = [ticket.Buyer(buyer_id=1, name="Alice", tel="1", id_card="id")]
        project = ticket.Project(
            project_id=100,
            name="show",
            screens=[
                ticket.Screen(
                    screen_id=200,
                    name="screen",
                    skus=[ticket.TicketSku(300, "vip", 8800, "sale", 1)],
                )
            ],
        )
        handle_risk = AsyncMock(return_value=True)

        with (
            patch("app.grabber.BiliClient", FakeBiliClient),
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
            patch(
                "app.grabber.ticket.prepare_order",
                new=AsyncMock(
                    side_effect=[
                        ticket.PrepareResult(
                            code=-352,
                            message="风控校验失败",
                            v_voucher="vv-test",
                        ),
                        ok_prepare(),
                    ]
                ),
            ),
            patch(
                "app.grabber.ticket.create_order",
                new=AsyncMock(return_value=ticket.CreateResult(code=100009, message="库存不足")),
            ),
            patch("app.grabber.Grabber._handle_risk", new=handle_risk),
            patch("app.grabber.asyncio.sleep", new=AsyncMock()),
        ):
            await grabber._run()

        handle_risk.assert_awaited_once_with("vv-test", {})
        self.assertEqual(grabber.status.attempts, 2)

    async def test_prepare_sold_out_without_monitor_uses_interval_retry(self) -> None:
        grabber = Grabber(self.make_config())

        result = await grabber._handle_prepare_failure(
            ticket.PrepareResult(code=100009, message="库存不足"),
            attempt=1,
            extra_params={},
        )

        self.assertIs(result.outcome, AttemptOutcome.RETRY)
        self.assertEqual(result.retry_delay, 0.1)
        self.assertEqual(grabber.status.retry_delay_ms, 100)

    async def test_prepare_rate_limit_uses_conservative_backoff(self) -> None:
        config = self.make_config()
        config.interval_ms = 100
        config.rate_limit_backoff_ms = 2500
        grabber = Grabber(config)

        result = await grabber._handle_prepare_failure(
            ticket.PrepareResult(code=429, message="请求过于频繁"),
            attempt=1,
            extra_params={},
        )

        self.assertIs(result.outcome, AttemptOutcome.RETRY)
        self.assertEqual(result.retry_delay, 2.5)
        self.assertEqual(grabber.status.retry_reason, "请求过频/风控拦截")

    async def test_create_order_exception_retries_until_max_attempts(self) -> None:
        events: list[dict] = []
        grabber = Grabber(self.make_config(), on_event=events.append)
        buyers = [ticket.Buyer(buyer_id=1, name="Alice", tel="1", id_card="id")]
        project = ticket.Project(
            project_id=100,
            name="show",
            screens=[
                ticket.Screen(
                    screen_id=200,
                    name="screen",
                    skus=[ticket.TicketSku(300, "vip", 8800, "sale", 1)],
                )
            ],
        )
        create_order = AsyncMock(side_effect=[RuntimeError("timeout"), RuntimeError("reset")])

        with (
            patch("app.grabber.BiliClient", FakeBiliClient),
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
            patch(
                "app.grabber.ticket.prepare_order",
                new=AsyncMock(return_value=ok_prepare()),
            ),
            patch("app.grabber.ticket.create_order", new=create_order),
            patch("app.grabber.asyncio.sleep", new=AsyncMock()),
        ):
            await grabber._run()

        self.assertEqual(create_order.await_count, 2)
        self.assertEqual(grabber.status.attempts, 2)
        self.assertEqual(grabber.status.finished_reason, "达到最大尝试次数")
        self.assertEqual(grabber.status.network_errors, 2)
        self.assertGreaterEqual(grabber.status.last_attempt_ms, 0)
        self.assertGreaterEqual(grabber.status.avg_attempt_ms, 0)
        self.assertTrue(any("create 异常" in event.get("message", "") for event in events))

    async def test_create_network_errors_use_exponential_backoff(self) -> None:
        config = self.make_config()
        config.max_attempts = 3
        grabber = Grabber(config)
        buyers = [ticket.Buyer(buyer_id=1, name="Alice", tel="1", id_card="id")]
        project = ticket.Project(
            project_id=100,
            name="show",
            screens=[
                ticket.Screen(
                    screen_id=200,
                    name="screen",
                    skus=[ticket.TicketSku(300, "vip", 8800, "sale", 1)],
                )
            ],
        )

        with (
            patch("app.grabber.BiliClient", FakeBiliClient),
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
            patch(
                "app.grabber.ticket.prepare_order",
                new=AsyncMock(return_value=ok_prepare()),
            ),
            patch(
                "app.grabber.ticket.create_order",
                new=AsyncMock(side_effect=[RuntimeError("timeout"), RuntimeError("reset"), RuntimeError("again")]),
            ),
            patch("app.grabber.asyncio.sleep", new=AsyncMock()) as sleep_mock,
        ):
            await grabber._run()

        self.assertEqual([args.args[0] for args in sleep_mock.await_args_list], [0.2, 0.4])
        self.assertEqual(grabber.status.retry_reason, "")
        self.assertEqual(grabber.status.retry_delay_ms, 0)

    async def test_rate_limit_create_records_retry_status(self) -> None:
        events: list[dict] = []
        grabber = Grabber(self.make_config(), on_event=events.append)
        buyers = [ticket.Buyer(buyer_id=1, name="Alice", tel="1", id_card="id")]
        project = ticket.Project(
            project_id=100,
            name="show",
            screens=[
                ticket.Screen(
                    screen_id=200,
                    name="screen",
                    skus=[ticket.TicketSku(300, "vip", 8800, "sale", 1)],
                )
            ],
        )

        with (
            patch("app.grabber.BiliClient", FakeBiliClient),
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
            patch(
                "app.grabber.ticket.prepare_order",
                new=AsyncMock(return_value=ok_prepare()),
            ),
            patch(
                "app.grabber.ticket.create_order",
                new=AsyncMock(return_value=ticket.CreateResult(code=429, message="请求过于频繁")),
            ),
            patch("app.grabber.asyncio.sleep", new=AsyncMock()) as sleep_mock,
        ):
            await grabber._run()

        sleep_mock.assert_awaited_once_with(2.0)
        self.assertEqual(grabber.status.retry_reason, "")
        self.assertEqual(grabber.status.retry_delay_ms, 0)

    async def test_initialization_resolves_buyers_and_price_before_start_time(self) -> None:
        events: list[dict] = []
        config = self.make_config()
        config.max_attempts = 1
        config.prewarm_seconds = 30
        config.start_time = self.future_time()
        grabber = Grabber(config, on_event=events.append)
        buyers = [ticket.Buyer(buyer_id=1, name="Alice", tel="1", id_card="id")]
        project = ticket.Project(
            project_id=100,
            name="show",
            screens=[
                ticket.Screen(
                    screen_id=200,
                    name="screen",
                    skus=[ticket.TicketSku(300, "vip", 8800, "sale", 1)],
                )
            ],
        )

        with (
            patch("app.grabber.BiliClient", FakeBiliClient),
            patch("app.grabber.Grabber._wait_until_prewarm", new=AsyncMock()) as wait_prewarm,
            patch("app.grabber.Grabber._wait_until_start", new=AsyncMock()) as wait_start,
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)) as get_buyers,
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)) as get_project,
            patch(
                "app.grabber.ticket.prepare_order",
                new=AsyncMock(return_value=ok_prepare()),
            ),
            patch(
                "app.grabber.ticket.create_order",
                new=AsyncMock(return_value=ticket.CreateResult(code=100009, message="库存不足")),
            ),
        ):
            await grabber._run()

        wait_prewarm.assert_awaited_once()
        wait_start.assert_awaited_once()
        get_buyers.assert_awaited_once()
        get_project.assert_awaited_once()
        messages = [event.get("message", "") for event in events]
        preheat_index = messages.index("开始抢票预热")
        ready_index = next(i for i, message in enumerate(messages) if message.startswith("购票人 1 人"))
        start_index = messages.index("开始抢票")
        self.assertLess(preheat_index, ready_index)
        self.assertLess(ready_index, start_index)

    async def test_pending_order_message_triggers_payment_notification(self) -> None:
        events: list[dict] = []
        grabber = Grabber(self.make_config(), on_event=events.append)
        buyers = [ticket.Buyer(buyer_id=1, name="Alice", tel="1", id_card="id")]
        project = ticket.Project(
            project_id=100,
            name="show",
            screens=[
                ticket.Screen(
                    screen_id=200,
                    name="screen",
                    skus=[ticket.TicketSku(300, "vip", 8800, "sale", 1)],
                )
            ],
        )

        with (
            patch("app.grabber.BiliClient", FakeBiliClient),
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
            patch(
                "app.grabber.ticket.prepare_order",
                new=AsyncMock(return_value=ok_prepare()),
            ),
            patch(
                "app.grabber.ticket.create_order",
                new=AsyncMock(
                    return_value=ticket.CreateResult(
                        code=100048,
                        message="你有尚未完成订单，请先支付",
                    )
                ),
            ),
            patch("app.grabber.notify.send_all", new=AsyncMock()) as send_all,
        ):
            await grabber._run()

        self.assertTrue(grabber.status.success)
        self.assertEqual(grabber.status.finished_reason, "订单已生成")
        send_all.assert_awaited_once()
        _, kwargs = send_all.await_args
        self.assertEqual(kwargs["title"], "会员购订单待支付")
        self.assertIn("请尽快前往 B 站订单页支付", kwargs["body"])

    async def test_success_order_id_notification_mentions_payment_window(self) -> None:
        events: list[dict] = []
        grabber = Grabber(self.make_config(), on_event=events.append)
        buyers = [ticket.Buyer(buyer_id=1, name="Alice", tel="1", id_card="id")]
        project = ticket.Project(
            project_id=100,
            name="show",
            screens=[
                ticket.Screen(
                    screen_id=200,
                    name="screen",
                    skus=[ticket.TicketSku(300, "vip", 8800, "sale", 1)],
                )
            ],
        )

        with (
            patch("app.grabber.BiliClient", FakeBiliClient),
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
            patch(
                "app.grabber.ticket.prepare_order",
                new=AsyncMock(return_value=ok_prepare()),
            ),
            patch(
                "app.grabber.ticket.create_order",
                new=AsyncMock(
                    return_value=ticket.CreateResult(
                        code=0,
                        message="下单成功",
                        order_id="ORDER123",
                    )
                ),
            ),
            patch("app.grabber.notify.send_all", new=AsyncMock()) as send_all,
        ):
            await grabber._run()

        self.assertTrue(grabber.status.success)
        self.assertEqual(grabber.status.order_id, "ORDER123")
        send_all.assert_awaited_once()
        _, kwargs = send_all.await_args
        self.assertEqual(kwargs["title"], "会员购订单待支付")
        self.assertEqual(kwargs["body"], "订单 ORDER123 已生成，请在 10 分钟内支付")

    def test_return_monitor_stock_detection_uses_count_and_sale_flag(self) -> None:
        grabber = Grabber(self.make_config())

        self.assertFalse(grabber._sku_looks_available(ticket.TicketSku(300, "vip", 8800, "售罄", 1)))
        self.assertTrue(grabber._sku_looks_available(ticket.TicketSku(300, "vip", 8800, "立即购买", 0)))
        self.assertFalse(grabber._sku_looks_available(ticket.TicketSku(300, "vip", 8800, "已售罄", 0)))
        self.assertFalse(grabber._sku_looks_available(ticket.TicketSku(300, "vip", 8800, "暂未开售", 0)))

    async def test_return_monitor_requires_future_end_time(self) -> None:
        config = self.make_config()
        config.return_monitor_enabled = True
        config.monitor_end_time = "2020-01-01T00:00:00"
        grabber = Grabber(config)

        with patch("app.grabber.BiliClient", FakeBiliClient):
            await grabber._run()

        self.assertFalse(grabber.status.running)
        self.assertIn("监控截止时间已过期", grabber.status.finished_reason)

    async def test_return_monitor_waits_without_prepare_when_no_stock(self) -> None:
        config = self.make_config()
        config.return_monitor_enabled = True
        config.monitor_end_time = self.future_time()
        grabber = Grabber(config)
        buyers = [ticket.Buyer(buyer_id=1, name="Alice", tel="1", id_card="id")]
        project = ticket.Project(
            project_id=100,
            name="show",
            screens=[
                ticket.Screen(
                    screen_id=200,
                    name="screen",
                    skus=[ticket.TicketSku(300, "vip", 8800, "已售罄", 0)],
                )
            ],
        )

        with (
            patch("app.grabber.BiliClient", FakeBiliClient),
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
            patch("app.grabber.Grabber._wait_for_return_ticket", new=AsyncMock(return_value=False)),
            patch("app.grabber.ticket.prepare_order", new=AsyncMock()) as prepare_order,
        ):
            await grabber._run()

        prepare_order.assert_not_awaited()

    async def test_sold_out_after_return_monitor_goes_back_to_monitoring(self) -> None:
        config = self.make_config()
        config.return_monitor_enabled = True
        config.monitor_end_time = self.future_time()
        grabber = Grabber(config)
        buyers = [ticket.Buyer(buyer_id=1, name="Alice", tel="1", id_card="id")]
        project = ticket.Project(
            project_id=100,
            name="show",
            screens=[
                ticket.Screen(
                    screen_id=200,
                    name="screen",
                    skus=[ticket.TicketSku(300, "vip", 8800, "销售中", 1)],
                )
            ],
        )
        wait_for_return = AsyncMock(side_effect=[True, False])

        with (
            patch("app.grabber.BiliClient", FakeBiliClient),
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
            patch("app.grabber.Grabber._wait_for_return_ticket", new=wait_for_return),
            patch(
                "app.grabber.ticket.prepare_order",
                new=AsyncMock(return_value=ok_prepare()),
            ),
            patch(
                "app.grabber.ticket.create_order",
                new=AsyncMock(return_value=ticket.CreateResult(code=100009, message="库存不足")),
            ) as create_order,
        ):
            await grabber._run()

        self.assertEqual(wait_for_return.await_count, 2)
        create_order.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
