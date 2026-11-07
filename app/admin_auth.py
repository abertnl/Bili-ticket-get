"""管理端 token、来源校验和认证失败限流。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hmac
import time
from urllib.parse import urlparse

from fastapi import Request, WebSocket
from fastapi.responses import Response

from .config import AppConfig, effective_admin_token

ADMIN_COOKIE_NAME = "ticket_buy_admin"
AUTH_BODY_MAX_BYTES = 1024
AUTH_FAILURE_LIMIT = 5
AUTH_FAILURE_WINDOW_SECONDS = 60.0


def _bearer_token(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value
    return None


def auth_client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def set_admin_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        max_age=60 * 60 * 12,
    )


class AdminAuth:
    """每次校验读取当前配置；失败计数归属于本应用实例。"""

    def __init__(self, get_config: Callable[[], AppConfig]) -> None:
        self._get_config = get_config
        self.failures: dict[str, list[float]] = {}

    def token(self) -> str:
        return effective_admin_token(self._get_config())

    def token_matches(self, value: str | None) -> bool:
        token = self.token()
        if not token or value is None:
            return False
        return hmac.compare_digest(value, token)

    def _carries_token(self, cookies: Mapping[str, str], headers: Mapping[str, str]) -> bool:
        """cookie、X-Admin-Token、Bearer 三个来源任一匹配即可。"""

        return (
            self.token_matches(cookies.get(ADMIN_COOKIE_NAME))
            or self.token_matches(headers.get("x-admin-token"))
            or self.token_matches(_bearer_token(headers.get("authorization")))
        )

    def request_authorized(self, request: Request) -> bool:
        return self._carries_token(request.cookies, request.headers)

    def websocket_authorized(self, websocket: WebSocket) -> bool:
        return self.origin_allowed(
            websocket.headers.get("origin"), websocket.headers.get("host")
        ) and self._carries_token(websocket.cookies, websocket.headers)

    def origin_allowed(self, origin: str | None, host_header: str | None) -> bool:
        if not origin:
            return True
        if not host_header:
            return False
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"}:
            return False
        normalized = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        if normalized in self._get_config().server.allowed_origins:
            return True
        return parsed.netloc.lower() == host_header.split(",", 1)[0].strip().lower()

    def _failures_for(self, client_key: str) -> list[float]:
        now = time.monotonic()
        failures = [ts for ts in self.failures.get(client_key, []) if now - ts <= AUTH_FAILURE_WINDOW_SECONDS]
        self.failures[client_key] = failures
        return failures

    def rate_limited(self, client_key: str) -> bool:
        return len(self._failures_for(client_key)) >= AUTH_FAILURE_LIMIT

    def record_failure(self, client_key: str) -> None:
        failures = self._failures_for(client_key)
        failures.append(time.monotonic())
        self.failures[client_key] = failures

    def clear_failures(self, client_key: str) -> None:
        self.failures.pop(client_key, None)
