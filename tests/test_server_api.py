"""服务端路由与配置接口回归测试。"""

import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import server
from app.admin_auth import ADMIN_COOKIE_NAME, AUTH_FAILURE_LIMIT
from app.config import AppConfig, NotifyConfig, ServerConfig


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
        server.admin_auth.failures.clear()
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
        plain_client.cookies.set(ADMIN_COOKIE_NAME, "stale-token")

        response = plain_client.get("/")
        index_response = plain_client.get("/index.html")

        self.assertEqual(response.status_code, 200)
        self.assertIn("管理验证", response.text)
        self.assertNotIn("/app.js", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(index_response.status_code, 200)
        self.assertIn("管理验证", index_response.text)
        self.assertNotIn("/app.js", index_response.text)
        self.assertEqual(index_response.headers["cache-control"], "no-store")
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

        for _ in range(AUTH_FAILURE_LIMIT):
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

    def test_return_monitor_sku_ids_round_trip_through_config(self) -> None:
        with patch("app.server.save_config"):
            response = self.client.post(
                "/api/config",
                json={"return_monitor_sku_ids": [301, 302, 301]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.state.config.return_monitor_sku_ids, [301, 302])
        self.assertEqual(response.json()["config"]["return_monitor_sku_ids"], [301, 302])

    def test_config_rejects_invalid_values(self) -> None:
        cases = [
            {"count": 0},
            {"interval_ms": 99},
            {"max_attempts": 0},
            {"prewarm_seconds": -1},
            {"rate_limit_backoff_ms": 999},
            {"rate_limit_cooldown_ms": 999},
            {"network_backoff_max_ms": 99},
            {"monitor_interval_ms": 999},
            {"captcha_mode": "bad"},
            {"buyer_ids": [1, 0]},
            {"return_monitor_sku_ids": [300, 0]},
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

    def test_grab_report_returns_empty_summary_without_run(self) -> None:
        response = self.client.get("/api/grab/report")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["run_id"], "")
        self.assertEqual(response.json()["effective_order_attempts"], 0)
        self.assertEqual(response.json()["payment_url"], "")
        self.assertEqual(response.json()["time_offset_ms"], 0)
        self.assertEqual(response.json()["transport"], "")
