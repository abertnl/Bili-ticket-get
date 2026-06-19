from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import notify
from app import server
from app.bili.captcha import RrocrSolver
from app.bili import auth, ticket
from app.bili.errors import ResultKind, classify
from app.config import AppConfig, NotifyConfig, ServerConfig
from app.grabber import AttemptOutcome, Grabber, _has_pending_order


class ServerConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.admin_token = "0123456789abcdef"
        server.state.config = AppConfig(
            cookie="SESSDATA=secret; bili_jct=csrf; DedeUserID=1",
            rrocr_token="rrocr-secret",
            notify=NotifyConfig(
                bark_url="https://api.day.app/bark-secret",
                serverchan_key="SCT-secret",
                imessage_recipient="+15555550123",
            ),
            server=ServerConfig(admin_token=self.admin_token),
        )
        server.state.grabber = None
        server._auth_failures.clear()
        self.client = TestClient(server.app, headers={"X-Admin-Token": self.admin_token})

    def test_config_response_redacts_sensitive_values(self) -> None:
        response = self.client.get("/api/config")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertNotIn("cookie", data)
        self.assertNotIn("rrocr_token", data)
        self.assertEqual(
            data["notify"],
            {"bark_url": "***", "serverchan_key": "***", "imessage_recipient": "***"},
        )
        self.assertTrue(data["has_cookie"])
        self.assertTrue(data["has_rrocr_token"])
        self.assertEqual(
            data["notify_configured"],
            {"bark_url": True, "serverchan_key": True, "imessage_recipient": True},
        )

    def test_api_requires_admin_token(self) -> None:
        server.state.config = AppConfig(
            server=ServerConfig(admin_token=self.admin_token),
        )
        plain_client = TestClient(server.app)

        response = plain_client.get("/api/config")
        authorized = plain_client.get(
            "/api/config",
            headers={"X-Admin-Token": self.admin_token},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["ok"], False)
        self.assertEqual(authorized.status_code, 200)
        self.assertNotIn("admin_token", authorized.json()["server"])

    def test_token_form_sets_admin_cookie(self) -> None:
        server.state.config = AppConfig(
            server=ServerConfig(admin_token=self.admin_token),
        )
        plain_client = TestClient(server.app)

        login_page = plain_client.get("/")
        bad = plain_client.post("/auth/token", data={"token": "wrong"})
        good = plain_client.post("/auth/token", data={"token": self.admin_token})
        response = plain_client.get("/api/config")

        self.assertEqual(login_page.status_code, 200)
        self.assertIn("管理验证", login_page.text)
        self.assertEqual(bad.status_code, 401)
        self.assertEqual(good.status_code, 200)
        self.assertEqual(response.status_code, 200)

    def test_static_frontend_requires_fresh_admin_cookie(self) -> None:
        server.state.config = AppConfig(
            server=ServerConfig(admin_token=self.admin_token),
        )
        plain_client = TestClient(server.app)
        plain_client.cookies.set(server.ADMIN_COOKIE_NAME, "stale-token")

        response = plain_client.get("/index.html")

        self.assertEqual(response.status_code, 200)
        self.assertIn("管理验证", response.text)
        self.assertNotIn("会员购抢票工具</h1>", response.text)

    def test_websocket_requires_admin_token(self) -> None:
        server.state.config = AppConfig(
            server=ServerConfig(admin_token=self.admin_token),
        )
        plain_client = TestClient(server.app)

        with self.assertRaises(WebSocketDisconnect) as cm:
            with plain_client.websocket_connect("/ws"):
                pass

        self.assertEqual(cm.exception.code, 1008)

        with plain_client.websocket_connect("/ws", headers={"X-Admin-Token": self.admin_token}):
            pass

    def test_cross_origin_api_and_websocket_are_rejected(self) -> None:
        server.state.config = AppConfig(
            server=ServerConfig(admin_token=self.admin_token),
        )
        plain_client = TestClient(server.app)

        response = plain_client.get(
            "/api/config",
            headers={"X-Admin-Token": self.admin_token, "Origin": "https://evil.example"},
        )
        self.assertEqual(response.status_code, 403)

        with self.assertRaises(WebSocketDisconnect) as cm:
            with plain_client.websocket_connect(
                "/ws",
                headers={"X-Admin-Token": self.admin_token, "Origin": "https://evil.example"},
            ):
                pass

        self.assertEqual(cm.exception.code, 1008)

    def test_configured_allowed_origin_can_access_api_and_websocket(self) -> None:
        server.state.config = AppConfig(
            server=ServerConfig(
                admin_token=self.admin_token,
                allowed_origins=["https://proxy.example"],
            ),
        )
        plain_client = TestClient(server.app)

        response = plain_client.get(
            "/api/config",
            headers={"X-Admin-Token": self.admin_token, "Origin": "https://proxy.example"},
        )
        self.assertEqual(response.status_code, 200)

        with plain_client.websocket_connect(
            "/ws",
            headers={"X-Admin-Token": self.admin_token, "Origin": "https://proxy.example"},
        ):
            pass

    def test_token_auth_limits_failures_and_body_size(self) -> None:
        server.state.config = AppConfig(
            server=ServerConfig(admin_token=self.admin_token),
        )
        plain_client = TestClient(server.app)

        too_large = plain_client.post("/auth/token", content="token=" + ("x" * 2048))
        self.assertEqual(too_large.status_code, 413)

        for _ in range(server.AUTH_FAILURE_LIMIT):
            response = plain_client.post("/auth/token", data={"token": "wrong"})
            self.assertEqual(response.status_code, 401)

        limited = plain_client.post("/auth/token", data={"token": "wrong"})
        self.assertEqual(limited.status_code, 429)

    def test_empty_rrocr_token_preserves_existing_value(self) -> None:
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

    def test_notify_placeholders_preserve_values_and_empty_fields_clear_channels(self) -> None:
        with patch("app.server.save_config"):
            response = self.client.post(
                "/api/config",
                json={
                    "notify": {
                        "bark_url": "***",
                        "serverchan_key": "",
                        "imessage_recipient": "***",
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.state.config.notify.bark_url, "https://api.day.app/bark-secret")
        self.assertEqual(server.state.config.notify.serverchan_key, "")
        self.assertEqual(server.state.config.notify.imessage_recipient, "+15555550123")
        self.assertEqual(
            response.json()["config"]["notify"],
            {"bark_url": "***", "serverchan_key": "", "imessage_recipient": "***"},
        )

    def test_custom_https_bark_url_is_allowed(self) -> None:
        with patch("app.server.save_config"):
            response = self.client.post(
                "/api/config",
                json={"notify": {"bark_url": "https://push.example.com:8443/bark-secret"}},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.state.config.notify.bark_url, "https://push.example.com:8443/bark-secret")
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
            {"notify": {"bark_url": "http://api.day.app/bark-secret"}},
            {"notify": {"bark_url": "https://127.0.0.1/bark-secret"}},
            {"notify": {"serverchan_key": "../SCT-secret"}},
        ]

        for payload in cases:
            with self.subTest(payload=payload):
                response = self.client.post("/api/config", json=payload)
                self.assertEqual(response.status_code, 422)

    def test_allowed_origin_rejects_paths(self) -> None:
        with self.assertRaises(ValueError):
            ServerConfig(allowed_origins=["https://proxy.example/path"])

    def test_project_id_must_be_positive(self) -> None:
        response = self.client.get("/api/project?project_id=0")

        self.assertEqual(response.status_code, 422)

    def test_external_route_errors_are_structured(self) -> None:
        with patch("app.server.auth.generate_qr", new=AsyncMock(side_effect=RuntimeError("offline"))):
            response = self.client.get("/api/login/qr")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["ok"], False)
        self.assertIn("offline", response.json()["message"])

    def test_cookie_login_requires_sessdata_csrf_and_uid(self) -> None:
        with patch("app.server.auth.get_nav_info", new=AsyncMock(return_value={"is_login": True})) as get_nav_info:
            response = self.client.post(
                "/api/login/cookie",
                json={"cookie": "SESSDATA=secret; DedeUserID=1"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["ok"], False)
        self.assertIn("bili_jct", response.json()["message"])
        get_nav_info.assert_not_awaited()

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
            server=ServerConfig(admin_token=self.admin_token),
        )

        response = self.client.post("/api/grab/start")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["ok"], False)
        self.assertIn("监控截止时间格式无效", response.json()["message"])
        self.assertIsNone(server.state.grabber)


class NotifyTests(unittest.IsolatedAsyncioTestCase):
    def test_rrocr_solver_uses_https_endpoint(self) -> None:
        self.assertTrue(RrocrSolver.API.startswith("https://"))

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
            patch(
                "app.notify._send_bark",
                new=AsyncMock(
                    side_effect=RuntimeError(
                        "failed https://api.day.app/bark-secret/会员购订单待支付/请及时支付",
                    )
                ),
            ),
            patch("app.notify._send_imessage", new=AsyncMock()) as send_imessage,
        ):
            with self.assertRaises(RuntimeError) as cm:
                await notify.send_all(config, "会员购订单待支付", "请及时支付")

        send_imessage.assert_awaited_once()
        self.assertIn("Bark:", str(cm.exception))
        self.assertNotIn("bark-secret", str(cm.exception))
        self.assertIn("***", str(cm.exception))

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


class AuthQrTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_qr_requests_scan_web_source_and_fills_empty_from(self) -> None:
        instances = []

        class FakeResponse:
            def json(self) -> dict:
                return {
                    "code": 0,
                    "message": "OK",
                    "data": {
                        "qrcode_key": "qr-key",
                        "url": (
                            "https://account.bilibili.com/h5/account-h5/auth/scan-web"
                            "?navhide=1&callback=close&qrcode_key=qr-key&from="
                        ),
                    },
                }

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs) -> None:
                self.get_calls = []
                instances.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc) -> None:
                return None

            async def get(self, url: str, **kwargs):
                self.get_calls.append((url, kwargs))
                return FakeResponse()

        with (
            patch("app.bili.auth.httpx.AsyncClient", FakeAsyncClient),
            patch("app.bili.auth._render_qr", return_value="data:image/png;base64,test") as render_qr,
        ):
            qr = await auth.generate_qr()

        self.assertEqual(instances[0].get_calls[0][1]["params"], {"source": "scan-web"})
        parsed = urlparse(qr.url)
        self.assertEqual(parsed.netloc, "account.bilibili.com")
        self.assertEqual(parse_qs(parsed.query)["from"], ["scan-web"])
        render_qr.assert_called_once_with(qr.url)
        self.assertEqual(qr.image_base64, "data:image/png;base64,test")


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

    async def test_price_resolution_uses_selected_screen_and_sku(self) -> None:
        config = self.make_config()
        grabber = Grabber(config)
        project = ticket.Project(
            project_id=100,
            name="show",
            screens=[
                ticket.Screen(
                    screen_id=201,
                    name="wrong screen",
                    skus=[ticket.TicketSku(300, "vip-wrong", 99900, "sale", 1)],
                ),
                ticket.Screen(
                    screen_id=200,
                    name="selected screen",
                    skus=[ticket.TicketSku(300, "vip", 8800, "sale", 1)],
                ),
            ],
        )

        with (
            patch("app.grabber.BiliClient", FakeBiliClient),
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
        ):
            grabber._client = FakeBiliClient(config.cookie)
            price = await grabber._resolve_price()

        self.assertEqual(price, 8800)

    async def test_buyer_count_must_match_ticket_count(self) -> None:
        config = self.make_config()
        config.count = 2
        grabber = Grabber(config)
        buyers = [ticket.Buyer(buyer_id=1, name="Alice", tel="1", id_card="id")]

        with patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)):
            grabber._client = FakeBiliClient(config.cookie)
            with self.assertRaisesRegex(RuntimeError, "购票人数必须与购买数量一致"):
                await grabber._resolve_buyers()

    async def test_missing_buyer_id_stops_before_prepare(self) -> None:
        events: list[dict] = []
        config = self.make_config()
        config.buyer_ids = [1, 2]
        config.count = 2
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
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
            patch("app.grabber.ticket.prepare_order", new=AsyncMock()) as prepare_order,
        ):
            await grabber._run()

        prepare_order.assert_not_awaited()
        self.assertFalse(grabber.status.running)
        self.assertIn("未匹配到购票人: 2", grabber.status.finished_reason)

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

    async def test_duplicate_order_code_triggers_payment_notification(self) -> None:
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
                        message="已经下单，请勿重复下单",
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
        self.assertIn("已经下单，请勿重复下单", kwargs["body"])

    async def test_prepare_duplicate_order_code_triggers_payment_notification(self) -> None:
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
                    return_value=ticket.PrepareResult(
                        code=100048,
                        message="已经下单，请勿重复下单",
                    )
                ),
            ),
            patch("app.grabber.ticket.create_order", new=AsyncMock()) as create_order,
            patch("app.grabber.notify.send_all", new=AsyncMock()) as send_all,
        ):
            await grabber._run()

        create_order.assert_not_awaited()
        self.assertTrue(grabber.status.success)
        self.assertEqual(grabber.status.finished_reason, "订单已生成")
        send_all.assert_awaited_once()

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
