"""底层 HTTP 客户端：Cookie 解析、请求头、bili_ticket 生成。"""

from __future__ import annotations

import asyncio
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

# 读取演出/票档等信息时，这些状态码多为瞬态（限流/网关抖动），可短退避后重试
TRANSIENT_STATUS_CODES = frozenset({408, 412, 429, 500, 502, 503, 504})

GEN_WEB_TICKET_URL = "https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket"


class JsonResponseError(ValueError):
    """响应无法解析为 JSON。"""


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
        return await self._request_json_with_retry("GET", url, **kwargs)

    async def post_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        return await self._request_json_with_retry("POST", url, **kwargs)

    async def _request_json_with_retry(
        self,
        method: str,
        url: str,
        *,
        retries: int = 2,
        backoff: float = 0.3,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """发起请求并解析 JSON，遇瞬态错误（网络抖动/非 JSON 响应/限流状态码）时短退避重试。

        预热阶段拉取演出信息/票档时，B 站偏偷可能返回空 body 或 412/429 页面，
        导致 ``resp.json()`` 抛 ``JSONDecodeError``。这里有限次重试以避免单次抖动让整个抢票崩溃。
        """

        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = await self._client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                if resp.status_code in TRANSIENT_STATUS_CODES and attempt < retries:
                    last_exc = RuntimeError(f"HTTP {resp.status_code}")
                else:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        last_exc = JsonResponseError(
                            f"响应不是合法 JSON（HTTP {resp.status_code}）"
                        )
                        last_exc.__cause__ = exc
                        if attempt >= retries:
                            raise last_exc from exc
            if attempt < retries:
                await asyncio.sleep(backoff * (attempt + 1))
        assert last_exc is not None
        raise last_exc

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
