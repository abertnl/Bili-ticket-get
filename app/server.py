"""FastAPI 本地服务：登录/配置/抢票路由 + 静态页 + WebSocket 实时日志。"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import time
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator

from .bili import auth, ticket
from .bili.client import BiliClient
from .config import AppConfig, NotifyConfig, effective_admin_token, ensure_admin_token, load_config, save_config
from .grabber import Grabber

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
ADMIN_COOKIE_NAME = "ticket_buy_admin"
AUTH_BODY_MAX_BYTES = 1024
AUTH_FAILURE_LIMIT = 5
AUTH_FAILURE_WINDOW_SECONDS = 60.0
_auth_failures: dict[str, list[float]] = {}


class AppState:
    """全局运行态：配置、抢票任务、WebSocket 客户端。"""

    def __init__(self) -> None:
        self.config: AppConfig = load_config()
        ensure_admin_token(self.config)
        self.grabber: Grabber | None = None
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def broadcast(self, event: dict[str, Any]) -> None:
        """把事件投递给所有 WebSocket 客户端（在事件循环线程内调用）。"""

        if not self.loop:
            return
        for ws in list(self.clients):
            try:
                future = asyncio.run_coroutine_threadsafe(ws.send_json(event), self.loop)
            except Exception:  # noqa: BLE001
                self.clients.discard(ws)
                continue
            future.add_done_callback(lambda fut, sock=ws: self._cleanup_failed_send(sock, fut))

    def _cleanup_failed_send(self, ws: WebSocket, future: Future) -> None:
        try:
            future.result()
        except Exception:  # noqa: BLE001
            self.clients.discard(ws)

    def new_grabber(self) -> Grabber:
        self.grabber = Grabber(self.config, on_event=self.broadcast)
        return self.grabber


state = AppState()


# ---- 请求体模型 ----
class CookieLogin(BaseModel):
    cookie: str = Field(max_length=16384)


class CaptchaSubmit(BaseModel):
    validate_value: str = Field(alias="validate", max_length=2048)
    seccode: str = Field(max_length=2048)


class ConfigUpdate(BaseModel):
    """允许通过配置接口修改的字段白名单。"""

    project_id: int | None = Field(default=None, ge=0)
    screen_id: int | None = Field(default=None, ge=0)
    sku_id: int | None = Field(default=None, ge=0)
    buyer_ids: list[int] | None = None
    count: int | None = Field(default=None, ge=1)
    start_time: str | None = Field(default=None, max_length=64)
    interval_ms: int | None = Field(default=None, ge=100)
    max_attempts: int | None = Field(default=None, ge=1)
    prewarm_seconds: int | None = Field(default=None, ge=0)
    rate_limit_backoff_ms: int | None = Field(default=None, ge=1000)
    network_backoff_max_ms: int | None = Field(default=None, ge=100)
    return_monitor_enabled: bool | None = None
    monitor_interval_ms: int | None = Field(default=None, ge=1000)
    monitor_end_time: str | None = Field(default=None, max_length=64)
    captcha_mode: Literal["manual", "rrocr"] | None = None
    rrocr_token: str | None = Field(default=None, max_length=512)
    notify: NotifyConfig | None = None

    @field_validator("buyer_ids")
    @classmethod
    def validate_buyer_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and any(buyer_id <= 0 for buyer_id in value):
            raise ValueError("buyer_ids 必须是正整数")
        return value


def _public_config(config: AppConfig) -> dict[str, Any]:
    """返回给浏览器的配置，避免回显账号凭据和推送密钥。"""

    data = config.model_dump(exclude={"cookie", "rrocr_token"})
    data["server"].pop("admin_token", None)
    data["has_cookie"] = bool(config.cookie)
    data["has_rrocr_token"] = bool(config.rrocr_token)
    data["admin_token_required"] = _admin_token_required()
    data["notify"] = {"bark_url": "", "serverchan_key": "", "imessage_recipient": ""}
    data["notify_configured"] = {
        "bark_url": bool(config.notify.bark_url),
        "serverchan_key": bool(config.notify.serverchan_key),
        "imessage_recipient": bool(config.notify.imessage_recipient),
    }
    return data


def _error(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "message": message}, status_code=status_code)


def _admin_token_required() -> bool:
    return True


def _admin_token() -> str:
    return effective_admin_token(state.config)


def _token_matches(value: str | None) -> bool:
    token = _admin_token()
    if not token or value is None:
        return False
    return hmac.compare_digest(value, token)


def _bearer_token(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value
    return None


def _request_authorized(request: Request) -> bool:
    if not _admin_token_required():
        return True
    return (
        _token_matches(request.cookies.get(ADMIN_COOKIE_NAME))
        or _token_matches(request.headers.get("x-admin-token"))
        or _token_matches(_bearer_token(request.headers.get("authorization")))
    )


def _websocket_authorized(websocket: WebSocket) -> bool:
    if not _admin_token_required():
        return True
    return (
        _origin_allowed(websocket.headers.get("origin"), websocket.headers.get("host"))
        and (
            _token_matches(websocket.cookies.get(ADMIN_COOKIE_NAME))
            or _token_matches(websocket.headers.get("x-admin-token"))
            or _token_matches(_bearer_token(websocket.headers.get("authorization")))
        )
    )


def _origin_allowed(origin: str | None, host_header: str | None) -> bool:
    if not origin:
        return True
    if not host_header:
        return False
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"}:
        return False
    normalized = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    if normalized in state.config.server.allowed_origins:
        return True
    return parsed.netloc.lower() == host_header.split(",", 1)[0].strip().lower()


def _auth_client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _auth_failures_for(client_key: str) -> list[float]:
    now = time.monotonic()
    failures = [ts for ts in _auth_failures.get(client_key, []) if now - ts <= AUTH_FAILURE_WINDOW_SECONDS]
    _auth_failures[client_key] = failures
    return failures


def _auth_rate_limited(client_key: str) -> bool:
    return len(_auth_failures_for(client_key)) >= AUTH_FAILURE_LIMIT


def _record_auth_failure(client_key: str) -> None:
    failures = _auth_failures_for(client_key)
    failures.append(time.monotonic())
    _auth_failures[client_key] = failures


def _clear_auth_failures(client_key: str) -> None:
    _auth_failures.pop(client_key, None)


def _set_admin_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        max_age=60 * 60 * 12,
    )


def _login_page() -> Response:
    return Response(
        """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>管理验证</title></head>
<body style="font-family:system-ui,sans-serif;max-width:420px;margin:12vh auto;padding:0 20px;line-height:1.5">
  <h1>管理验证</h1>
  <form method="post" action="/auth/token">
    <label for="token">管理 token</label>
    <input id="token" name="token" type="password" autocomplete="current-password" required autofocus
      style="display:block;width:100%;box-sizing:border-box;margin:8px 0 16px;padding:10px">
    <button type="submit" style="padding:10px 14px">进入</button>
  </form>
</body>
</html>""",
        media_type="text/html; charset=utf-8",
    )


def _merge_sensitive_updates(updates: dict[str, Any]) -> dict[str, Any]:
    """空敏感字段表示前端未改动，保留旧值。"""

    if updates.get("rrocr_token") == "":
        updates.pop("rrocr_token")
    notify = updates.get("notify")
    if isinstance(notify, dict):
        current = state.config.notify.model_dump()
        for key, value in list(notify.items()):
            if value == "":
                notify[key] = current.get(key, "")
    return updates


def _parse_local_datetime(value: str, label: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label}格式无效") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def _validate_grab_config(config: AppConfig) -> str | None:
    """Return a user-facing error message when the task cannot start locally."""

    if not config.cookie:
        return "请先登录"
    if config.project_id <= 0:
        return "请先选择演出"
    if config.screen_id <= 0:
        return "请先选择场次"
    if config.sku_id <= 0:
        return "请先选择票档"
    if not config.buyer_ids:
        return "请先选择购票人"

    try:
        _parse_local_datetime(config.start_time, "开抢时间")
        monitor_end = _parse_local_datetime(config.monitor_end_time, "监控截止时间")
    except ValueError as exc:
        return str(exc)

    if config.return_monitor_enabled:
        if monitor_end is None:
            return "启用回流票监控时必须设置监控截止时间"
        if datetime.now(tz=monitor_end.tzinfo) >= monitor_end:
            return "监控截止时间已过期"
    return None


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    state.loop = asyncio.get_running_loop()
    yield
    if state.grabber and state.grabber.is_running():
        await state.grabber.stop()


app = FastAPI(title="B 站会员购抢票工具", lifespan=lifespan)


@app.middleware("http")
async def require_admin_token(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        if not _origin_allowed(request.headers.get("origin"), request.headers.get("host")):
            return _error("未授权：来源不被允许", 403)
        if not _request_authorized(request):
            return _error("未授权：请提供管理 token", 401)
    return await call_next(request)


# ---- 页面 ----
@app.get("/")
async def index(request: Request) -> Response:
    if not _request_authorized(request):
        return _login_page()
    return FileResponse(WEB_DIR / "index.html")


@app.post("/auth/token")
async def auth_token(request: Request) -> Response:
    if not _origin_allowed(request.headers.get("origin"), request.headers.get("host")):
        return _error("未授权：来源不被允许", 403)
    client_key = _auth_client_key(request)
    if _auth_rate_limited(client_key):
        return _error("认证失败次数过多，请稍后再试", 429)
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            parsed_length = int(content_length)
        except ValueError:
            return _error("Content-Length 无效", 400)
        if parsed_length > AUTH_BODY_MAX_BYTES:
            return _error("请求体过大", 413)
    body = (await request.body()).decode("utf-8", errors="replace")
    if len(body.encode("utf-8")) > AUTH_BODY_MAX_BYTES:
        return _error("请求体过大", 413)
    token = parse_qs(body).get("token", [""])[0]
    if not _token_matches(token):
        _record_auth_failure(client_key)
        return _error("管理 token 无效", 401)
    _clear_auth_failures(client_key)
    response = RedirectResponse("/", status_code=303)
    _set_admin_cookie(response, request, token)
    return response


# ---- 登录 ----
@app.get("/api/login/qr")
async def login_qr() -> Any:
    try:
        qr = await auth.generate_qr()
    except Exception as exc:  # noqa: BLE001
        return _error(f"生成二维码失败: {exc}", 502)
    return {"qrcode_key": qr.qrcode_key, "image": qr.image_base64}


@app.get("/api/login/poll")
async def login_poll(qrcode_key: str = Query(max_length=256)) -> Any:
    try:
        result = await auth.poll_qr(qrcode_key)
    except Exception as exc:  # noqa: BLE001
        return _error(f"轮询扫码状态失败: {exc}", 502)
    payload: dict[str, Any] = {"code": result.code, "message": result.message}
    if result.code == 0 and result.cookie:
        state.config.cookie = result.cookie
        save_config(state.config)
        try:
            payload["user"] = await auth.get_nav_info(result.cookie)
        except Exception:  # noqa: BLE001
            payload["user"] = {"is_login": True}
    return payload


@app.post("/api/login/cookie")
async def login_cookie(body: CookieLogin) -> JSONResponse:
    try:
        info = await auth.get_nav_info(body.cookie)
    except Exception as exc:  # noqa: BLE001
        return _error(f"校验 Cookie 失败: {exc}", 502)
    if not info["is_login"]:
        return _error("Cookie 无效或已过期", 400)
    state.config.cookie = body.cookie
    save_config(state.config)
    return JSONResponse({"ok": True, "user": info})


@app.get("/api/login/status")
async def login_status() -> dict[str, Any]:
    if not state.config.cookie:
        return {"is_login": False}
    try:
        return await auth.get_nav_info(state.config.cookie)
    except Exception:  # noqa: BLE001
        return {"is_login": False}


# ---- 配置 ----
@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return _public_config(state.config)


@app.post("/api/config")
async def update_config(body: ConfigUpdate) -> Any:
    # 只更新客户端显式传入的字段（排除 None），其余保留原值
    updates = _merge_sensitive_updates(body.model_dump(exclude_none=True))
    merged = state.config.model_dump()
    merged.update(updates)
    try:
        state.config = AppConfig.model_validate(merged)
    except ValidationError as exc:
        return _error(f"配置无效: {exc.errors()[0]['msg']}", 400)
    save_config(state.config)
    return {"ok": True, "config": _public_config(state.config)}


# ---- 演出 / 购票人 ----
@app.get("/api/project")
async def get_project(project_id: int = Query(ge=1)) -> Any:
    client = BiliClient(state.config.cookie)
    try:
        project = await ticket.get_project(client, project_id)
    except RuntimeError as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return _error(f"加载演出信息失败: {exc}", 502)
    finally:
        await client.aclose()
    return {
        "project_id": project.project_id,
        "name": project.name,
        "screens": [
            {
                "screen_id": s.screen_id,
                "name": s.name,
                "skus": [
                    {
                        "sku_id": k.sku_id,
                        "desc": k.desc,
                        "price": k.price,
                        "sale_flag": k.sale_flag,
                    }
                    for k in s.skus
                ],
            }
            for s in project.screens
        ],
    }


@app.get("/api/buyers")
async def get_buyers() -> JSONResponse:
    if not state.config.cookie:
        return JSONResponse(
            {"ok": False, "message": "请先登录，再加载购票人"},
            status_code=401,
        )
    client = BiliClient(state.config.cookie)
    try:
        buyers = await ticket.get_buyers(client)
    except RuntimeError as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return _error(f"加载购票人失败: {exc}", 502)
    finally:
        await client.aclose()
    return JSONResponse({
        "ok": True,
        "buyers": [
            {"buyer_id": b.buyer_id, "name": b.name, "tel": b.tel, "id_card": b.id_card}
            for b in buyers
        ]
    })


# ---- 抢票控制 ----
@app.post("/api/grab/start")
async def grab_start() -> Any:
    if state.grabber and state.grabber.is_running():
        return {"ok": False, "message": "已经在抢票中"}
    error = _validate_grab_config(state.config)
    if error:
        return _error(error, 400)
    grabber = state.new_grabber()
    await grabber.start()
    return {"ok": True}


@app.post("/api/grab/stop")
async def grab_stop() -> dict[str, Any]:
    if state.grabber:
        await state.grabber.stop()
    return {"ok": True}


@app.get("/api/grab/status")
async def grab_status() -> dict[str, Any]:
    if not state.grabber:
        return {"running": False}
    return state.grabber.status.to_dict()


# ---- 验证码（人工模式） ----
@app.get("/api/captcha")
async def captcha_pending() -> dict[str, Any]:
    if state.grabber:
        pending = state.grabber.captcha_pending()
        if pending:
            return {"pending": True, **pending}
    return {"pending": False}


@app.post("/api/captcha")
async def captcha_submit(body: CaptchaSubmit) -> dict[str, Any]:
    if state.grabber and state.grabber.submit_captcha(body.validate_value, body.seccode):
        return {"ok": True}
    return {"ok": False, "message": "当前没有待处理的验证码"}


# ---- WebSocket 实时日志 ----
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    if not _websocket_authorized(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    state.clients.add(websocket)
    try:
        while True:
            # 仅保持连接；客户端无需发送数据
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        state.clients.discard(websocket)


# 静态资源（放在最后，避免覆盖 API 路由）
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="static")
