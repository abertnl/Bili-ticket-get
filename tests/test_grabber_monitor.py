"""回流票监控与冲刺轮转回归测试。"""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import grab_targets
from app.bili import ticket
from app.grab_monitor import ReturnTicketMonitor
from app.grabber import Grabber, TicketTarget
from tests.helpers import FakeBiliClient, GrabberTestCase, make_context, ok_prepare


class GrabberMonitorTests(GrabberTestCase):
    async def test_initial_burst_rate_limit_wall_records_telemetry_and_enters_monitoring(self) -> None:
        config = self.make_config()
        config.return_monitor_enabled = True
        config.max_attempts = 10
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
        create_order = AsyncMock(return_value=ticket.CreateResult(code=429, message="HTTP 429"))
        wait_for_return = AsyncMock(return_value=False)

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("app.grabber.TELEMETRY_DIR", Path(tmp)),
                patch("app.grabber.BiliClient", FakeBiliClient),
                patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
                patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
                patch.object(ReturnTicketMonitor, "wait_for_return_ticket", new=wait_for_return),
                patch("app.grabber.ticket.prepare_order", new=AsyncMock(return_value=ok_prepare())),
                patch("app.grabber.ticket.create_order", new=create_order),
                patch("app.grabber.asyncio.sleep", new=AsyncMock()),
            ):
                await grabber._run()

            payloads = [
                json.loads(line)
                for line in Path(grabber.status.telemetry_path).read_text(encoding="utf-8").splitlines()
            ]

        burst_finishes = [item for item in payloads if item["event"] == "burst_finish"]
        self.assertEqual(create_order.await_count, 3)
        wait_for_return.assert_awaited_once()
        self.assertEqual(len(burst_finishes), 1)
        self.assertEqual(burst_finishes[0]["outcome"], "rate_limit_wall")
        self.assertEqual(burst_finishes[0]["burst_attempts"], 3)
        self.assertEqual(burst_finishes[0]["burst_rate_limits"], 3)

    def test_return_monitor_stock_detection_uses_count_and_sale_flag(self) -> None:
        config = self.make_config()

        def available(sale_flag: str, num: int) -> bool:
            return grab_targets.sku_looks_available(config, ticket.TicketSku(300, "vip", 8800, sale_flag, num))

        self.assertFalse(available("售罄", 1))
        self.assertTrue(available("立即购买", 0))
        self.assertFalse(available("已售罄", 0))
        self.assertFalse(available("暂未开售", 0))

    async def test_return_monitor_selects_highest_stock_candidate(self) -> None:
        config = self.make_config()
        config.return_monitor_enabled = True
        config.return_monitor_sku_ids = [301, 302]
        config.monitor_end_time = self.future_time()
        grabber = Grabber(config)
        grabber._client = FakeBiliClient(config.cookie)
        project = ticket.Project(
            project_id=100,
            name="show",
            screens=[
                ticket.Screen(
                    screen_id=200,
                    name="screen",
                    skus=[
                        ticket.TicketSku(300, "primary", 8800, "已售罄", 0),
                        ticket.TicketSku(301, "balcony", 6800, "销售中", 1),
                        ticket.TicketSku(302, "vip", 9800, "销售中", 4),
                    ],
                )
            ],
        )

        with patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)):
            targets = await grabber._monitor.wait_for_return_ticket(
                datetime.now().astimezone() + timedelta(minutes=5)
            )

        self.assertEqual([target.sku_id for target in targets], [302, 301])
        self.assertEqual(grabber.status.monitor_target_count, 3)
        self.assertIn("primary", grabber.status.last_monitor_candidates)

    def test_return_monitor_ties_use_configured_order(self) -> None:
        config = self.make_config()
        config.return_monitor_sku_ids = [302, 301]
        project = ticket.Project(
            project_id=100,
            name="show",
            screens=[
                ticket.Screen(
                    screen_id=200,
                    name="screen",
                    skus=[
                        ticket.TicketSku(300, "primary", 8800, "已售罄", 0),
                        ticket.TicketSku(301, "balcony", 6800, "销售中", 2),
                        ticket.TicketSku(302, "vip", 9800, "销售中", 2),
                    ],
                )
            ],
        )

        targets = make_context(config).available_monitor_targets(project)

        self.assertEqual([target.sku_id for target in targets], [302, 301])

    async def test_return_monitor_requires_future_end_time(self) -> None:
        config = self.make_config()
        config.return_monitor_enabled = True
        config.monitor_end_time = "2020-01-01T00:00:00"
        grabber = Grabber(config)

        with patch("app.grabber.BiliClient", FakeBiliClient):
            await grabber._run()

        self.assertFalse(grabber.status.running)
        self.assertIn("监控截止时间已过期", grabber.status.finished_reason)

    async def test_return_monitor_stops_without_extra_prepare_when_no_stock_after_initial_burst(self) -> None:
        config = self.make_config()
        config.return_monitor_enabled = True
        config.max_attempts = 4
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
            patch.object(ReturnTicketMonitor, "wait_for_return_ticket", new=AsyncMock(return_value=False)) as wait_for_return,
            patch("app.grabber.ticket.prepare_order", new=AsyncMock(return_value=ok_prepare())) as prepare_order,
            patch(
                "app.grabber.ticket.create_order",
                new=AsyncMock(return_value=ticket.CreateResult(code=100009, message="库存不足")),
            ),
            patch("app.grabber.asyncio.sleep", new=AsyncMock()),
        ):
            await grabber._run()

        self.assertEqual(prepare_order.await_count, 2)
        wait_for_return.assert_awaited_once()

    async def test_sold_out_after_return_monitor_goes_back_to_monitoring(self) -> None:
        config = self.make_config()
        config.return_monitor_enabled = True
        config.sold_out_burst_attempts = 1
        config.max_attempts = 5
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
        wait_for_return = AsyncMock(
            side_effect=[[TicketTarget(200, 300, "vip", 8800, "销售中", 1, 0)], False]
        )

        with (
            patch("app.grabber.BiliClient", FakeBiliClient),
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
            patch.object(ReturnTicketMonitor, "wait_for_return_ticket", new=wait_for_return),
            patch(
                "app.grabber.ticket.prepare_order",
                new=AsyncMock(return_value=ok_prepare()),
            ),
            patch(
                "app.grabber.ticket.create_order",
                new=AsyncMock(return_value=ticket.CreateResult(code=100009, message="库存不足")),
            ) as create_order,
            patch("app.grabber.asyncio.sleep", new=AsyncMock()),
        ):
            await grabber._run()

        self.assertEqual(wait_for_return.await_count, 2)
        self.assertEqual(create_order.await_count, 3)

    async def test_return_burst_falls_back_to_next_available_candidate(self) -> None:
        config = self.make_config()
        config.return_monitor_enabled = True
        config.sold_out_burst_attempts = 1
        config.max_attempts = 6
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
                    skus=[
                        ticket.TicketSku(300, "primary", 8800, "销售中", 1),
                        ticket.TicketSku(301, "balcony", 6800, "销售中", 1),
                        ticket.TicketSku(302, "vip", 9800, "销售中", 2),
                    ],
                )
            ],
        )
        return_targets = [
            TicketTarget(200, 301, "balcony", 6800, "销售中", 1, 0),
            TicketTarget(200, 302, "vip", 9800, "销售中", 2, 1),
        ]
        wait_for_return = AsyncMock(return_value=return_targets)

        async def create_side_effect(*args, **kwargs):
            sku_id = args[3]
            if sku_id == 302:
                return ticket.CreateResult(code=0, message="下单成功", order_id="ORDER302")
            return ticket.CreateResult(code=100009, message="库存不足")

        create_order = AsyncMock(side_effect=create_side_effect)

        with (
            patch("app.grabber.BiliClient", FakeBiliClient),
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
            patch.object(ReturnTicketMonitor, "wait_for_return_ticket", new=wait_for_return),
            patch("app.grabber.ticket.prepare_order", new=AsyncMock(return_value=ok_prepare())),
            patch("app.grabber.ticket.create_order", new=create_order),
            patch("app.grabber.asyncio.sleep", new=AsyncMock()),
        ):
            await grabber._run()

        attempted_skus = [call.args[3] for call in create_order.await_args_list]
        self.assertEqual(attempted_skus[-2:], [301, 302])
        wait_for_return.assert_awaited_once()
        self.assertTrue(grabber.status.success)
        self.assertEqual(grabber.status.order_id, "ORDER302")

    async def test_return_burst_refreshes_candidates_after_100001(self) -> None:
        config = self.make_config()
        config.return_monitor_enabled = True
        config.return_monitor_sku_ids = [301, 302]
        config.sold_out_burst_attempts = 1
        config.max_attempts = 6
        config.monitor_end_time = self.future_time()
        grabber = Grabber(config)
        buyers = [ticket.Buyer(buyer_id=1, name="Alice", tel="1", id_card="id")]
        initial_project = ticket.Project(
            project_id=100,
            name="show",
            screens=[
                ticket.Screen(
                    screen_id=200,
                    name="screen",
                    skus=[
                        ticket.TicketSku(300, "primary", 8800, "销售中", 1),
                        ticket.TicketSku(301, "balcony", 6800, "销售中", 1),
                        ticket.TicketSku(302, "vip", 9800, "已售罄", 0),
                    ],
                )
            ],
        )
        refreshed_project = ticket.Project(
            project_id=100,
            name="show",
            screens=[
                ticket.Screen(
                    screen_id=200,
                    name="screen",
                    skus=[
                        ticket.TicketSku(300, "primary", 8800, "已售罄", 0),
                        ticket.TicketSku(301, "balcony", 6800, "已售罄", 0),
                        ticket.TicketSku(302, "vip", 9800, "销售中", 5),
                    ],
                )
            ],
        )
        wait_for_return = AsyncMock(
            return_value=[TicketTarget(200, 301, "balcony", 6800, "销售中", 1, 0)]
        )

        async def create_side_effect(*args, **kwargs):
            sku_id = args[3]
            if sku_id == 301:
                return ticket.CreateResult(code=100001, message="暂无可售票")
            if sku_id == 302:
                return ticket.CreateResult(code=0, message="下单成功", order_id="ORDER302")
            return ticket.CreateResult(code=100009, message="库存不足")

        create_order = AsyncMock(side_effect=create_side_effect)

        with (
            patch("app.grabber.BiliClient", FakeBiliClient),
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
            patch(
                "app.grabber.ticket.get_project",
                new=AsyncMock(side_effect=[initial_project, refreshed_project]),
            ),
            patch.object(ReturnTicketMonitor, "wait_for_return_ticket", new=wait_for_return),
            patch("app.grabber.ticket.prepare_order", new=AsyncMock(return_value=ok_prepare())),
            patch("app.grabber.ticket.create_order", new=create_order),
            patch("app.grabber.asyncio.sleep", new=AsyncMock()),
        ):
            await grabber._run()

        attempted_skus = [call.args[3] for call in create_order.await_args_list]
        self.assertIn(301, attempted_skus)
        self.assertEqual(attempted_skus[-1], 302)
        self.assertTrue(grabber.status.success)

    async def test_return_monitor_runs_initial_burst_before_monitoring(self) -> None:
        config = self.make_config()
        config.return_monitor_enabled = True
        config.sold_out_burst_attempts = 6
        config.max_attempts = 10
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
        wait_for_return = AsyncMock(return_value=False)
        create_order = AsyncMock(return_value=ticket.CreateResult(code=100009, message="库存不足"))

        with (
            patch("app.grabber.BiliClient", FakeBiliClient),
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
            patch.object(ReturnTicketMonitor, "wait_for_return_ticket", new=wait_for_return),
            patch("app.grabber.ticket.prepare_order", new=AsyncMock(return_value=ok_prepare())),
            patch("app.grabber.ticket.create_order", new=create_order),
            patch("app.grabber.asyncio.sleep", new=AsyncMock()),
        ):
            await grabber._run()

        self.assertEqual(create_order.await_count, 2)
        wait_for_return.assert_awaited_once()
        self.assertEqual(grabber.status.sold_out_count, 2)

    async def test_sold_out_bursts_n_times_before_returning_to_monitor(self) -> None:
        config = self.make_config()
        config.return_monitor_enabled = True
        config.sold_out_burst_attempts = 3
        config.max_attempts = 10
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
        wait_for_return = AsyncMock(
            side_effect=[[TicketTarget(200, 300, "vip", 8800, "销售中", 1, 0)], False]
        )
        create_order = AsyncMock(return_value=ticket.CreateResult(code=100009, message="库存不足"))

        with (
            patch("app.grabber.BiliClient", FakeBiliClient),
            patch("app.grabber.ticket.get_buyers", new=AsyncMock(return_value=buyers)),
            patch("app.grabber.ticket.get_project", new=AsyncMock(return_value=project)),
            patch.object(ReturnTicketMonitor, "wait_for_return_ticket", new=wait_for_return),
            patch("app.grabber.ticket.prepare_order", new=AsyncMock(return_value=ok_prepare())),
            patch("app.grabber.ticket.create_order", new=create_order),
            patch("app.grabber.asyncio.sleep", new=AsyncMock()),
        ):
            await grabber._run()

        self.assertEqual(create_order.await_count, 5)
        self.assertEqual(wait_for_return.await_count, 2)
        self.assertEqual(grabber.status.sold_out_count, 5)
