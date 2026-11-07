"""抢中后的支付信息、推送与复盘回归测试。"""

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.bili import ticket
from app.grabber import Grabber
from tests.helpers import FakeBiliClient, GrabberTestCase, ok_prepare


class GrabberPaymentTests(GrabberTestCase):
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
            patch("app.grabber.ticket.get_pay_qrcode_url", new=AsyncMock(return_value="https://pay.example/qr")),
            patch("app.grabber.notify.send_all", new=AsyncMock()) as send_all,
        ):
            await grabber._run()

        self.assertTrue(grabber.status.success)
        self.assertEqual(grabber.status.order_id, "ORDER123")
        self.assertEqual(grabber.status.payment_url, ticket.get_order_detail_url("ORDER123"))
        self.assertEqual(grabber.status.pay_qrcode_url, "https://pay.example/qr")
        send_all.assert_awaited_once()
        _, kwargs = send_all.await_args
        self.assertEqual(kwargs["title"], "会员购订单待支付")
        self.assertIn("订单 ORDER123 已生成，请在 10 分钟内支付", kwargs["body"])
        self.assertIn(ticket.get_order_detail_url("ORDER123"), kwargs["body"])

    async def test_run_writes_sanitized_telemetry_and_report_summary(self) -> None:
        events: list[dict] = []
        config = self.make_config()
        config.max_attempts = 1
        buyers = [ticket.Buyer(buyer_id=1, name="Alice", tel="15555550123", id_card="secret-id")]
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

        with tempfile.TemporaryDirectory() as tmp:
            grabber = Grabber(config, on_event=events.append)
            with (
                patch("app.grabber.TELEMETRY_DIR", Path(tmp)),
                patch("app.grabber.BiliClient", FakeBiliClient),
                patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
                patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
                patch("app.grabber.ticket.prepare_order", new=AsyncMock(return_value=ok_prepare())),
                patch(
                    "app.grabber.ticket.create_order",
                    new=AsyncMock(return_value=ticket.CreateResult(code=100009, message="库存不足")),
                ),
            ):
                await grabber._run()

            telemetry_path = Path(grabber.status.telemetry_path)
            self.assertTrue(grabber.status.run_id)
            self.assertEqual(grabber.status.phase, "finished")
            self.assertTrue(telemetry_path.exists())
            payloads = [json.loads(line) for line in telemetry_path.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(item["event"] == "attempt_result" for item in payloads))
            telemetry_text = telemetry_path.read_text(encoding="utf-8")
            self.assertNotIn("SESSDATA", telemetry_text)
            self.assertNotIn("Alice", telemetry_text)
            self.assertNotIn("15555550123", telemetry_text)
            self.assertNotIn("secret-id", telemetry_text)

            report = grabber.report()
            self.assertEqual(report["run_id"], grabber.status.run_id)
            self.assertEqual(report["telemetry_path"], str(telemetry_path))
            self.assertEqual(report["effective_order_attempts"], 1)
            self.assertEqual(report["sold_out_count"], 1)
