"""限速退避与 prepare token 复用回归测试。"""

import time
from unittest.mock import AsyncMock, patch

from app.bili import ticket
from app.grabber import AttemptOutcome, Grabber, TicketTarget
from tests.helpers import FakeBiliClient, GrabberTestCase, ok_prepare


class GrabberPacingTests(GrabberTestCase):
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

    async def test_repeated_rate_limits_enter_cooldown(self) -> None:
        config = self.make_config()
        config.interval_ms = 1000
        config.max_interval_ms = 3000
        config.max_attempts = 4
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
            patch("app.grabber.ticket.prepare_order", new=AsyncMock(return_value=ok_prepare())),
            patch(
                "app.grabber.ticket.create_order",
                new=AsyncMock(return_value=ticket.CreateResult(code=429, message="HTTP 429")),
            ),
            patch("app.grabber.asyncio.sleep", new=AsyncMock()) as sleep_mock,
        ):
            await grabber._run()

        self.assertEqual([args.args[0] for args in sleep_mock.await_args_list], [2.0, 3.0, 8.0])
        self.assertEqual(grabber.status.rate_limit_count, 4)
        self.assertEqual(grabber.status.finished_reason, "达到最大尝试次数")

    def test_prepare_token_cache_is_scoped_to_sku(self) -> None:
        grabber = Grabber(self.make_config())
        grabber._cached_token = "token-301"
        grabber._cached_ptoken = "ptoken"
        grabber._cached_token_at = time.perf_counter()
        grabber._cached_token_sku_id = 301

        self.assertEqual(
            grabber._reusable_token(TicketTarget(200, 301, "balcony", 6800)),
            "token-301",
        )
        self.assertEqual(
            grabber._reusable_token(TicketTarget(200, 302, "vip", 9800)),
            "",
        )

    def test_aimd_backs_off_on_rate_limit_and_speeds_up_on_retry(self) -> None:
        config = self.make_config()
        config.interval_ms = 500
        config.max_interval_ms = 3000
        grabber = Grabber(config)

        self.assertAlmostEqual(grabber._dynamic_interval, 0.5)
        grabber._on_rate_limit()
        self.assertAlmostEqual(grabber._dynamic_interval, 1.0)
        grabber._on_rate_limit()
        self.assertAlmostEqual(grabber._dynamic_interval, 2.0)
        self.assertEqual(grabber.status.rate_limit_count, 2)
        self.assertEqual(grabber.status.consecutive_rate_limits, 2)
        self.assertEqual(grabber._rate_limit_recovery_remaining, 10)

        grabber._on_retryable()
        self.assertAlmostEqual(grabber._dynamic_interval, 2.0)
        self.assertEqual(grabber.status.consecutive_rate_limits, 0)
        self.assertEqual(grabber._rate_limit_recovery_remaining, 9)

        for _ in range(9):
            grabber._on_retryable()
        self.assertAlmostEqual(grabber._dynamic_interval, 2.0)
        self.assertEqual(grabber._rate_limit_recovery_remaining, 0)

        grabber._on_retryable()
        self.assertAlmostEqual(grabber._dynamic_interval, 1.975)

        for _ in range(200):
            grabber._on_retryable()
        self.assertAlmostEqual(grabber._dynamic_interval, 0.5)

        for _ in range(200):
            grabber._on_rate_limit()
        self.assertAlmostEqual(grabber._dynamic_interval, 3.0)

    def test_aimd_disabled_keeps_fixed_interval(self) -> None:
        config = self.make_config()
        config.interval_ms = 500
        config.adaptive_rate_enabled = False
        grabber = Grabber(config)

        grabber._on_rate_limit()
        self.assertAlmostEqual(grabber._dynamic_interval, 0.5)
        self.assertAlmostEqual(grabber._retry_delay(), 0.5)

    async def test_prepare_token_reused_on_congestion(self) -> None:
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
        create_order = AsyncMock(return_value=ticket.CreateResult(code=100001, message="系统繁忙"))

        with (
            patch("app.grabber.BiliClient", FakeBiliClient),
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
            patch("app.grabber.ticket.prepare_order", new=prepare_order),
            patch("app.grabber.ticket.create_order", new=create_order),
            patch("app.grabber.asyncio.sleep", new=AsyncMock()),
        ):
            await grabber._run()

        self.assertEqual(prepare_order.await_count, 1)
        self.assertEqual(create_order.await_count, 2)
        self.assertEqual(grabber.status.congestion_count, 2)

    async def test_prepare_token_not_reused_after_rate_limit(self) -> None:
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
        create_order = AsyncMock(return_value=ticket.CreateResult(code=429, message="请求过于频繁"))

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
