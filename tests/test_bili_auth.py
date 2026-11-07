"""扫码登录回归测试。"""

import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from app.bili import auth


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
