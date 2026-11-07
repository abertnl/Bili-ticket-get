"""会员购下单接口载荷回归测试。"""

import unittest
from unittest.mock import AsyncMock, patch

from app.bili import ticket
from tests.helpers import _FakeResponse


class TicketPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_project_uses_new_detail_endpoint_with_stock_count(self) -> None:
        class FakeTicketClient:
            def __init__(self) -> None:
                self.post_json = AsyncMock(
                    return_value={
                        "code": 0,
                        "data": {
                            "projectId": 100,
                            "projectName": "show",
                            "screenList": [
                                {
                                    "screenId": 200,
                                    "name": "screen",
                                    "ticketList": [
                                        {
                                            "skuId": 300,
                                            "desc": "vip",
                                            "price": 8800,
                                            "saleFlag": {"display_name": "销售中", "number": 6},
                                            "stockNum": 5,
                                        }
                                    ],
                                }
                            ],
                        },
                    }
                )
                self.get_json = AsyncMock()

        client = FakeTicketClient()

        project = await ticket.get_project(client, 100)  # type: ignore[arg-type]

        self.assertEqual(project.project_id, 100)
        self.assertEqual(project.name, "show")
        self.assertEqual(project.screens[0].skus[0].sku_id, 300)
        self.assertEqual(project.screens[0].skus[0].num, 5)
        client.get_json.assert_not_awaited()

    async def test_get_project_falls_back_to_old_detail_endpoint(self) -> None:
        class FakeTicketClient:
            def __init__(self) -> None:
                self.post_json = AsyncMock(side_effect=RuntimeError("new down"))
                self.get_json = AsyncMock(
                    return_value={
                        "code": 0,
                        "data": {
                            "id": 100,
                            "name": "show",
                            "screen_list": [
                                {
                                    "id": 200,
                                    "name": "screen",
                                    "ticket_list": [
                                        {
                                            "id": 300,
                                            "desc": "vip",
                                            "price": 8800,
                                            "sale_flag": 4,
                                            "num": 0,
                                        }
                                    ],
                                }
                            ],
                        },
                    }
                )

        client = FakeTicketClient()

        project = await ticket.get_project(client, 100)  # type: ignore[arg-type]

        self.assertEqual(project.screens[0].skus[0].sale_flag, "售罄")
        self.assertEqual(project.screens[0].skus[0].num, 0)
        client.get_json.assert_awaited_once()

    async def test_prepare_order_includes_request_limit_hint_and_parses_ptoken(self) -> None:
        class FakeTicketClient:
            csrf = "csrf-token"

            def __init__(self) -> None:
                self.post = AsyncMock(
                    return_value=_FakeResponse(
                        200,
                        payload={
                            "code": 0,
                            "data": {"token": "prepare-token", "ptoken": "ptoken=="},
                        },
                    )
                )

        client = FakeTicketClient()
        buyers = [ticket.Buyer(buyer_id=1, name="Alice", tel="155", id_card="id")]

        result = await ticket.prepare_order(client, 100, 200, 300, 1, buyers)  # type: ignore[arg-type]

        self.assertEqual(result.token, "prepare-token")
        self.assertEqual(result.ptoken, "ptoken==")
        _, kwargs = client.post.await_args
        payload = kwargs["json"]
        self.assertIs(payload["ignoreRequestLimit"], True)
        self.assertEqual(payload["requestSource"], "neul-next")
        self.assertIs(payload["newRisk"], True)

    async def test_create_order_transmits_ptoken_and_compatibility_fields(self) -> None:
        class FakeTicketClient:
            csrf = "csrf-token"

            def __init__(self) -> None:
                self.post = AsyncMock(
                    return_value=_FakeResponse(
                        200,
                        payload={"code": 0, "data": {"orderId": "ORDER123"}},
                    )
                )

        client = FakeTicketClient()
        buyers = [ticket.Buyer(buyer_id=1, name="Alice", tel="155", id_card="id")]

        with patch("app.bili.ticket.time.time", return_value=123.456):
            result = await ticket.create_order(
                client,  # type: ignore[arg-type]
                100,
                200,
                300,
                1,
                "prepare-token",
                buyers,
                8800,
                ptoken="ptoken==",
            )

        self.assertEqual(result.order_id, "ORDER123")
        _, kwargs = client.post.await_args
        self.assertEqual(kwargs["params"]["ptoken"], "ptoken")
        body = kwargs["json"]
        self.assertEqual(body["ptoken"], "ptoken")
        self.assertEqual(body["again"], 1)
        self.assertEqual(body["timestamp"], 123456)
        self.assertIs(body["newRisk"], True)
        self.assertEqual(body["requestSource"], "neul-next")
        self.assertEqual(body["orderCreateUrl"], ticket.CREATE_URL)

    async def test_create_order_transmits_contact_fields(self) -> None:
        class FakeTicketClient:
            csrf = "csrf-token"

            def __init__(self) -> None:
                self.post = AsyncMock(
                    return_value=_FakeResponse(
                        200,
                        payload={"code": 0, "data": {"orderId": "ORDER123"}},
                    )
                )

        client = FakeTicketClient()
        buyers = [ticket.Buyer(buyer_id=1, name="Alice", tel="155", id_card="id")]

        await ticket.create_order(
            client,  # type: ignore[arg-type]
            100,
            200,
            300,
            1,
            "prepare-token",
            buyers,
            8800,
            contact_name="Carol",
            contact_tel="18800001111",
        )

        _, kwargs = client.post.await_args
        body = kwargs["json"]
        self.assertEqual(body["buyer"], "Carol")
        self.assertEqual(body["tel"], "18800001111")

    async def test_create_order_uses_first_buyer_as_contact_fallback(self) -> None:
        class FakeTicketClient:
            csrf = "csrf-token"

            def __init__(self) -> None:
                self.post = AsyncMock(return_value=_FakeResponse(200, payload={"code": 0}))

        client = FakeTicketClient()
        buyers = [
            ticket.Buyer(buyer_id=1, name="Alice", tel="18800001111", id_card="id"),
            ticket.Buyer(buyer_id=2, name="Bob", tel="18800002222", id_card="id"),
        ]

        await ticket.create_order(
            client,  # type: ignore[arg-type]
            100,
            200,
            300,
            2,
            "prepare-token",
            buyers,
            8800,
        )

        _, kwargs = client.post.await_args
        body = kwargs["json"]
        self.assertEqual(body["buyer"], "Alice")
        self.assertEqual(body["tel"], "18800001111")

    async def test_pay_qrcode_url_reads_pay_param_code_url(self) -> None:
        class FakeTicketClient:
            def __init__(self) -> None:
                self.get_json = AsyncMock(
                    return_value={"code": 0, "data": {"code_url": "https://pay.example/qr"}}
                )

        client = FakeTicketClient()

        url = await ticket.get_pay_qrcode_url(client, "ORDER123")  # type: ignore[arg-type]

        self.assertEqual(url, "https://pay.example/qr")
        client.get_json.assert_awaited_once_with(ticket.PAY_PARAM_URL, params={"order_id": "ORDER123"})
