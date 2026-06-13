"""底层 HTTP 客户端：Cookie 解析、请求头、bili_ticket 生成。"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import httpx

# 移动端 UA，会员购票务接口对移动端 UA 兼容更好
DEFAULT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
    "Mobile/15E148 Safari/604.1"
)

DEFAULT_MAX_CONNECTIONS = 10
DEFAULT_MAX_KEEPALIVE_CONNECTIONS = 5

GEN_WEB_TICKET_URL = "https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket"


def parse_cookie(cookie_str: str) -> dict[str, str]:
    """把原始 Cookie 字符串解析为 dict。"""

    jar: dict[str, str] = {}
    if not cookie_str:
        return jar
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        jar[key.strip()] = value.strip()
    return jar


class BiliClient:
    """封装一个带会员购默认配置的 httpx 异步客户端。"""

    def __init__(
        self,
        cookie: str,
        timeout: float = 10.0,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        max_keepalive_connections: int = DEFAULT_MAX_KEEPALIVE_CONNECTIONS,
    ) -> None:
        self.cookies = parse_cookie(cookie)
        self.timeout = timeout
        timeout_config = httpx.Timeout(
            timeout,
            connect=min(timeout, 5.0),
            read=timeout,
            write=timeout,
            pool=min(timeout, 5.0),
        )
        limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            keepalive_expiry=30.0,
        )
        self._client = httpx.AsyncClient(
            timeout=timeout_config,
            limits=limits,
            headers={
                "User-Agent": DEFAULT_UA,
                "Referer": "https://show.bilibili.com/",
                "Accept": "application/json, text/plain, */*",
                "Connection": "keep-alive",
            },
            cookies=self.cookies,
            follow_redirects=True,
        )

    # ---- 凭据快捷访问 ----
    @property
    def csrf(self) -> str:
        """CSRF Token，即 Cookie 中的 bili_jct。"""

        return self.cookies.get("bili_jct", "")

    @property
    def uid(self) -> str:
        """用户 ID，即 Cookie 中的 DedeUserID。"""

        return self.cookies.get("DedeUserID", "")

    @property
    def sessdata(self) -> str:
        return self.cookies.get("SESSDATA", "")

    @property
    def is_logged_in(self) -> bool:
        return bool(self.sessdata and self.csrf and self.uid)

    def set_cookie(self, key: str, value: str) -> None:
        """运行时追加/更新单个 Cookie（如 x-bili-gaia-vtoken）。"""

        self.cookies[key] = value
        self._client.cookies.set(key, value)

    # ---- 请求封装 ----
    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._client.get(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._client.post(url, **kwargs)

    async def get_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        resp = await self._client.get(url, **kwargs)
        return resp.json()

    async def post_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        resp = await self._client.post(url, **kwargs)
        return resp.json()

    # ---- bili_ticket（降低风控触发概率） ----
    async def gen_bili_ticket(self) -> str | None:
        """生成并写入 bili_ticket（有效期约 3 天）。

        参考 bilibili-API-collect：HMAC-SHA256，key=``XgwSnGZ1p``，
        message=``"ts" + 时间戳``。
        """

        ts = int(time.time())
        hexsign = hmac.new(
            b"XgwSnGZ1p",
            f"ts{ts}".encode(),
            hashlib.sha256,
        ).hexdigest()
        try:
            data = await self.post_json(
                GEN_WEB_TICKET_URL,
                params={
                    "key_id": "ec02",
                    "hexsign": hexsign,
                    "context[ts]": str(ts),
                    "csrf": self.csrf,
                },
            )
        except (httpx.HTTPError, ValueError):
            return None
        if data.get("code") == 0:
            ticket = data.get("data", {}).get("ticket")
            if ticket:
                self.set_cookie("bili_ticket", ticket)
                return ticket
        return None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "BiliClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()
