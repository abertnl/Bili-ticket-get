"""预热与购票人/票价解析回归测试。"""

from unittest.mock import AsyncMock, patch

from app.bili import ticket
from app.grabber import Grabber
from tests.helpers import FakeBiliClient, GrabberTestCase, ok_prepare


class GrabberPrepareTests(GrabberTestCase):
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

    async def test_prewarm_resolution_retries_transient_failure(self) -> None:
        config = self.make_config()
        grabber = Grabber(config)
        grabber._client = FakeBiliClient(config.cookie)
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
        get_project = AsyncMock(
            side_effect=[RuntimeError("Expecting value: line 1 column 1 (char 0)"), project]
        )

        with (
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
            patch("app.grabber.ticket.get_project", new=get_project),
            patch("app.grabber.asyncio.sleep", new=AsyncMock()) as sleep_mock,
        ):
            resolved_buyers, price = await grabber._resolve_prewarm_data()

        self.assertEqual(price, 8800)
        self.assertEqual(len(resolved_buyers), 1)
        self.assertEqual(get_project.await_count, 2)
        sleep_mock.assert_awaited()

    async def test_prewarm_resolution_does_not_retry_config_errors(self) -> None:
        config = self.make_config()
        config.buyer_ids = [1, 2]
        config.count = 2
        grabber = Grabber(config)
        grabber._client = FakeBiliClient(config.cookie)
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
        get_buyers = AsyncMock(return_value=buyers)

        with (
            patch("app.grabber.ticket.get_buyers", new=get_buyers),
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
            patch("app.grabber.asyncio.sleep", new=AsyncMock()) as sleep_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "未匹配到购票人"):
                await grabber._resolve_prewarm_data()

        self.assertEqual(get_buyers.await_count, 1)
        sleep_mock.assert_not_awaited()
