"""BiliClient 韧性回归测试。"""

import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, patch

from app.bili.client import BiliClient
from tests.helpers import _FakeResponse


class ClientResilienceTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self) -> BiliClient:
        return BiliClient("SESSDATA=s; bili_jct=c; DedeUserID=1")

    async def test_sync_server_time_uses_response_date_header(self) -> None:
        client = self.make_client()
        date_header = format_datetime(datetime.fromtimestamp(1001, tz=timezone.utc), usegmt=True)
        with (
            patch.object(
                client._client,
                "head",
                new=AsyncMock(return_value=_FakeResponse(200, headers={"date": date_header})),
            ),
            patch("app.bili.client.time.time", side_effect=[1000.0, 1000.2]),
        ):
            offset_ms = await client.sync_server_time()
        self.assertEqual(offset_ms, 900)
        await client.aclose()

    async def test_prewarm_connection_returns_status_without_raising(self) -> None:
        client = self.make_client()
        with patch.object(
            client._client,
            "head",
            new=AsyncMock(return_value=_FakeResponse(204)),
        ) as head:
            ok = await client.prewarm_connection()
        self.assertTrue(ok)
        head.assert_awaited_once()
        await client.aclose()

    async def test_get_json_retries_non_json_response(self) -> None:
        client = self.make_client()
        request = AsyncMock(
            side_effect=[
                _FakeResponse(200, raise_json=True),
                _FakeResponse(200, payload={"code": 0}),
            ]
        )
        with (
            patch.object(client._client, "request", new=request),
            patch("app.bili.client.asyncio.sleep", new=AsyncMock()),
        ):
            data = await client.get_json("https://example.invalid/x")
        self.assertEqual(data, {"code": 0})
        self.assertEqual(request.await_count, 2)
        await client.aclose()

    async def test_get_json_retries_transient_status(self) -> None:
        client = self.make_client()
        request = AsyncMock(
            side_effect=[
                _FakeResponse(429),
                _FakeResponse(200, payload={"code": 0}),
            ]
        )
        with (
            patch.object(client._client, "request", new=request),
            patch("app.bili.client.asyncio.sleep", new=AsyncMock()),
        ):
            data = await client.get_json("https://example.invalid/x")
        self.assertEqual(data, {"code": 0})
        self.assertEqual(request.await_count, 2)
        await client.aclose()

    async def test_gen_bili_ticket_ignores_non_json_response(self) -> None:
        client = self.make_client()
        request = AsyncMock(return_value=_FakeResponse(200, raise_json=True))
        with (
            patch.object(client._client, "request", new=request),
            patch("app.bili.client.asyncio.sleep", new=AsyncMock()),
        ):
            ticket_value = await client.gen_bili_ticket()
        self.assertIsNone(ticket_value)
        self.assertEqual(request.await_count, 3)
        await client.aclose()

    async def test_get_json_raises_after_exhausting_retries(self) -> None:
        client = self.make_client()
        request = AsyncMock(return_value=_FakeResponse(200, raise_json=True))
        with (
            patch.object(client._client, "request", new=request),
            patch("app.bili.client.asyncio.sleep", new=AsyncMock()),
        ):
            with self.assertRaises(ValueError):
                await client.get_json("https://example.invalid/x")
        self.assertEqual(request.await_count, 3)
        await client.aclose()


if __name__ == "__main__":
    unittest.main()
