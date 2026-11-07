"""FastAPI 本地服务：登录/配置/抢票路由 + 静态页 + WebSocket 实时日志。"""

from __future__ import annotations

import asyncio
import contextlib
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .bili import auth, ticket
from .bili.client import BiliClient
from .admin_auth import (
    ADMIN_COOKIE_NAME,
    AUTH_BODY_MAX_BYTES,
    AdminAuth,
    auth_client_key,
    set_admin_cookie,
)
from .api_schemas import CaptchaSubmit, ConfigUpdate, CookieLogin
from .config import AppConfig, ensure_admin_token, load_config, save_config
from .config_service import merge_sensitive_updates, public_config, validate_grab_config
from .grab_reporting import build_report
from .grabber import Grabber

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


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
admin_auth = AdminAuth(lambda: state.config)


def _error(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "message": message}, status_code=status_code)


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
        headers={"Cache-Control": "no-store"},
    )


def _login_page_with_cleared_cookie() -> Response:
    response = _login_page()
    response.delete_cookie(ADMIN_COOKIE_NAME)
    return response


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    state.loop = asyncio.get_running_loop()
    yield
    if state.grabber and state.grabber.is_running():
        await state.grabber.stop()


app = FastAPI(title="B 站会员购抢票工具", lifespan=lifespan)


@app.middleware("http")
async def require_admin_token(request: Request, call_next):
    if request.url.path == "/auth/token":
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        if not admin_auth.origin_allowed(request.headers.get("origin"), request.headers.get("host")):
            return _error("未授权：来源不被允许", 403)
        if not admin_auth.request_authorized(request):
            return _error("未授权：请提供管理 token", 401)
    elif not admin_auth.request_authorized(request):
        return _login_page_with_cleared_cookie()
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# ---- 页面 ----
@app.get("/")
async def index(request: Request) -> Response:
    if not admin_auth.request_authorized(request):
        return _login_page_with_cleared_cookie()
    return FileResponse(WEB_DIR / "index.html")


@app.post("/auth/token")
async def auth_token(request: Request) -> Response:
    if not admin_auth.origin_allowed(request.headers.get("origin"), request.headers.get("host")):
        return _error("未授权：来源不被允许", 403)
    client_key = auth_client_key(request)
    if admin_auth.rate_limited(client_key):
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
    if not admin_auth.token_matches(token):
        admin_auth.record_failure(client_key)
        return _error("管理 token 无效", 401)
    admin_auth.clear_failures(client_key)
    response = RedirectResponse("/", status_code=303)
    set_admin_cookie(response, request, token)
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
    missing = auth.missing_required_cookie_keys(body.cookie)
    if missing:
        return _error(f"Cookie 缺少必需字段: {', '.join(missing)}", 400)
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
    return public_config(state.config)


@app.post("/api/config")
async def update_config(body: ConfigUpdate) -> Any:
    # 只更新客户端显式传入的字段（排除 None），其余保留原值
    updates = merge_sensitive_updates(state.config, body.model_dump(exclude_unset=True, exclude_none=True))
    merged = state.config.model_dump()
    merged.update(updates)
    try:
        state.config = AppConfig.model_validate(merged)
    except ValidationError as exc:
        return _error(f"配置无效: {exc.errors()[0]['msg']}", 400)
    save_config(state.config)
    return {"ok": True, "config": public_config(state.config)}


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
                        "num": k.num,
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
    error = validate_grab_config(state.config)
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


@app.get("/api/grab/report")
async def grab_report() -> dict[str, Any]:
    if not state.grabber:
        return build_report()
    return state.grabber.report()


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
    if not admin_auth.websocket_authorized(websocket):
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
