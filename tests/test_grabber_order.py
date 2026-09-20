"""下单尝试与错误码分支回归测试。"""

from unittest.mock import AsyncMock, patch

from app.bili import ticket
from app.grabber import AttemptOutcome, Grabber, _has_pending_order
from tests.helpers import FakeBiliClient, GrabberTestCase, ok_prepare


class GrabberOrderTests(GrabberTestCase):
    def test_has_pending_order_matches_known_payment_messages(self) -> None:
        self.assertTrue(_has_pending_order("你有尚未完成订单，请先支付"))
        self.assertTrue(_has_pending_order("存在待支付订单"))
        self.assertTrue(_has_pending_order("重复购买", 100003))
        self.assertTrue(_has_pending_order("重复订单", 100079))
        self.assertFalse(_has_pending_order("库存不足"))

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

    async def test_create_order_receives_prepare_ptoken(self) -> None:
        config = self.make_config()
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
        create_order = AsyncMock(return_value=ticket.CreateResult(code=100001, message="系统繁忙"))

        with (
            patch("app.grabber.BiliClient", FakeBiliClient),
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
            patch("app.grabber.ticket.prepare_order", new=AsyncMock(return_value=ok_prepare(ptoken="ptoken=="))),
            patch("app.grabber.ticket.create_order", new=create_order),
            patch("app.grabber.asyncio.sleep", new=AsyncMock()),
        ):
            await grabber._run()

        _, kwargs = create_order.await_args_list[0]
        self.assertEqual(kwargs["ptoken"], "ptoken==")

    async def test_prepare_expired_code_forces_reprepare(self) -> None:
        config = self.make_config()
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
        prepare_order = AsyncMock(return_value=ok_prepare())
        create_order = AsyncMock(
            side_effect=[
                ticket.CreateResult(code=100051, message="订单准备过期"),
                ticket.CreateResult(code=100009, message="库存不足"),
            ]
        )

        with (
            patch("app.grabber.BiliClient", FakeBiliClient),
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
            patch("app.grabber.ticket.prepare_order", new=prepare_order),
            patch("app.grabber.ticket.create_order", new=create_order),
            patch("app.grabber.asyncio.sleep", new=AsyncMock()),
        ):
            await grabber._run()

        self.assertEqual(prepare_order.await_count, 2)
        self.assertEqual(create_order.await_count, 2)

    async def test_price_error_updates_pay_money_before_retry(self) -> None:
        config = self.make_config()
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
        create_order = AsyncMock(
            side_effect=[
                ticket.CreateResult(code=100034, message="票价错误", pay_money=9900),
                ticket.CreateResult(code=100009, message="库存不足"),
            ]
        )

        with (
            patch("app.grabber.BiliClient", FakeBiliClient),
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
            patch("app.grabber.ticket.prepare_order", new=AsyncMock(return_value=ok_prepare())),
            patch("app.grabber.ticket.create_order", new=create_order),
            patch("app.grabber.asyncio.sleep", new=AsyncMock()),
        ):
            await grabber._run()

        first_args = create_order.await_args_list[0].args
        second_args = create_order.await_args_list[1].args
        self.assertEqual(first_args[7], 8800)
        self.assertEqual(second_args[7], 9900)
