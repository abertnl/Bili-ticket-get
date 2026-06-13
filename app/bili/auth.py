"""登录相关：扫码登录、Cookie 导入校验、当前用户信息。"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from typing import Any

import httpx
import qrcode

from .client import DEFAULT_UA, BiliClient

QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"


@dataclass
class QrCode:
    """二维码生成结果。"""

    qrcode_key: str
    url: str
    image_base64: str  # data:image/png;base64,... 可直接放进 <img src>


@dataclass
class QrPollResult:
    """扫码轮询结果。

    code: 0=成功 86038=二维码失效 86090=已扫码待确认 86101=未扫码
    """

    code: int
    message: str
    cookie: str = ""  # 成功时返回的完整 cookie 字符串


def _render_qr(url: str) -> str:
    """把链接渲染成 base64 PNG。"""

    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


async def generate_qr() -> QrCode:
    """生成扫码登录二维码。"""

    async with httpx.AsyncClient(headers={"User-Agent": DEFAULT_UA}, timeout=10.0) as client:
        resp = await client.get(QR_GENERATE_URL)
        data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"生成二维码失败: {data.get('message')}")
    payload = data["data"]
    return QrCode(
        qrcode_key=payload["qrcode_key"],
        url=payload["url"],
        image_base64=_render_qr(payload["url"]),
    )


async def poll_qr(qrcode_key: str) -> QrPollResult:
    """轮询扫码状态；成功时从 Set-Cookie 响应头中提取登录 cookie。"""

    async with httpx.AsyncClient(
        headers={"User-Agent": DEFAULT_UA}, timeout=10.0, follow_redirects=False,
    ) as client:
        resp = await client.get(QR_POLL_URL, params={"qrcode_key": qrcode_key})
        data = resp.json()
        inner = data.get("data", {})
        code = inner.get("code", -1)
        message = inner.get("message", "")
        cookie = ""
        if code == 0:
            # 登录成功，cookie 在响应头 Set-Cookie 中
            set_cookies = resp.headers.get_list("set-cookie")
            parts: list[str] = []
            for raw in set_cookies:
                # 只取 name=value 部分（忽略 Path/Domain/Expires 等属性）
                name_value = raw.split(";", 1)[0].strip()
                if "=" in name_value:
                    parts.append(name_value)
            cookie = "; ".join(parts)
    return QrPollResult(code=code, message=message, cookie=cookie)


async def get_nav_info(cookie: str) -> dict[str, Any]:
    """查询当前登录用户信息，用于校验 cookie 是否有效。

    返回 ``{"is_login": bool, "uname": str, "mid": int}``。
    """

    client = BiliClient(cookie)
    try:
        data = await client.get_json(NAV_URL)
    finally:
        await client.aclose()
    inner = data.get("data", {})
    return {
        "is_login": bool(inner.get("isLogin")),
        "uname": inner.get("uname", ""),
        "mid": inner.get("mid", 0),
    }
