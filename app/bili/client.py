"""底层 HTTP 客户端：Cookie 解析、请求头、bili_ticket 生成。"""

from __future__ import annotations

import asyncio
from datetime import timezone
from email.utils import parsedate_to_datetime
import hashlib
import hmac
import time
from typing import Any

import httpx

# 稳定桌面浏览器 UA；保持请求头一致性，不做随机指纹伪装。
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_MAX_CONNECTIONS = 10
DEFAULT_MAX_KEEPALIVE_CONNECTIONS = 5

# 读取演出/票档等信息时，这些状态码多为瞬态（限流/网关抖动），可短退避后重试
TRANSIENT_STATUS_CODES = frozenset({408, 412, 429, 500, 502, 503, 504})

GEN_WEB_TICKET_URL = "https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket"
SHOW_BASE_URL = "https://show.bilibili.com/"


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
        http2_enabled: bool = True,
    ) -> None:
        self.cookies = parse_cookie(cookie)
        self.timeout = timeout
        self.transport = "http1"
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
        client_kwargs: dict[str, Any] = {
            "timeout": timeout_config,
            "limits": limits,
            "headers": {
                "User-Agent": DEFAULT_UA,
                "Referer": "https://show.bilibili.com/",
                "Origin": "https://show.bilibili.com",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "sec-ch-ua": '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"macOS"',
            },
            "cookies": self.cookies,
            "follow_redirects": True,
        }
        if http2_enabled:
            try:
                self._client = httpx.AsyncClient(http2=True, **client_kwargs)
                self.transport = "http2"
            except ImportError:
                self._client = httpx.AsyncClient(http2=False, **client_kwargs)
        else:
            self._client = httpx.AsyncClient(http2=False, **client_kwargs)

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

    async def head(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._client.head(url, **kwargs)

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

    async def sync_server_time(self, url: str = SHOW_BASE_URL) -> int:
        """用服务端 Date 头估算本机时钟偏移，失败时返回 0 毫秒。"""

        started = time.time()
        try:
            resp = await self.head(url)
        except httpx.HTTPError:
            return 0
        ended = time.time()
        date_header = resp.headers.get("date", "")
        if not date_header:
            return 0
        try:
            parsed = parsedate_to_datetime(date_header)
        except (TypeError, ValueError, AttributeError, OverflowError):
            return 0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        server_time = parsed.timestamp()
        local_midpoint = (started + ended) / 2.0
        return int(round((server_time - local_midpoint) * 1000))

    async def prewarm_connection(self, url: str = SHOW_BASE_URL) -> bool:
        """预热 show.bilibili.com 连接；失败只影响返回值，不抛出。"""

        try:
            resp = await self.head(url)
        except httpx.HTTPError:
            return False
        return resp.status_code < 500

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
