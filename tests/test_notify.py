"""推送与验证码服务回归测试。"""

import unittest
from unittest.mock import AsyncMock, patch

from app import notify
from app.bili.captcha import RrocrSolver
from app.config import NotifyConfig


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
