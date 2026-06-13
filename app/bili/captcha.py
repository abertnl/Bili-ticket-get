"""极验(geetest)验证码求解：半自动人工 + 第三方打码（可插拔）。

设计上 ``CaptchaSolver`` 是一个协议，``RiskHandler`` 只依赖该协议；
默认提供 ``ManualSolver``（人工在网页里完成极验）与 ``RrocrSolver``（第三方打码）。
本工具不内置任何破解算法。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass
class GeetestChallenge:
    """极验挑战参数。"""

    gt: str
    challenge: str


@dataclass
class GeetestResult:
    """极验验证结果。"""

    validate: str
    seccode: str


class CaptchaSolver(Protocol):
    """验证码求解协议。"""

    async def solve(self, challenge: GeetestChallenge) -> GeetestResult: ...


class ManualSolver:
    """人工求解：把挑战暴露给网页前端，由用户完成极验后回填结果。

    工作流：
    1. ``RiskHandler`` 调用 :meth:`solve`，内部创建一个 Future 并记录当前挑战；
    2. Web 层通过 :meth:`current` 拿到 gt/challenge 渲染极验控件；
    3. 用户完成后，Web 层调用 :meth:`submit` 回填 validate/seccode，唤醒 Future。
    """

    def __init__(self, timeout: float = 180.0) -> None:
        self.timeout = timeout
        self._future: asyncio.Future[GeetestResult] | None = None
        self._challenge: GeetestChallenge | None = None

    @property
    def pending(self) -> bool:
        return self._future is not None and not self._future.done()

    def current(self) -> GeetestChallenge | None:
        """返回当前等待人工处理的挑战（供前端渲染）。"""

        return self._challenge if self.pending else None

    def submit(self, validate: str, seccode: str) -> bool:
        """前端回填验证结果。"""

        if self._future is not None and not self._future.done():
            self._future.set_result(GeetestResult(validate=validate, seccode=seccode))
            return True
        return False

    async def solve(self, challenge: GeetestChallenge) -> GeetestResult:
        loop = asyncio.get_running_loop()
        self._future = loop.create_future()
        self._challenge = challenge
        try:
            return await asyncio.wait_for(self._future, timeout=self.timeout)
        finally:
            self._challenge = None
            self._future = None


class RrocrSolver:
    """第三方打码服务（rrocr）示例实现。

    需要用户自备 token。仅作为可插拔示例，使用第三方服务请自行评估合规与风险。
    """

    API = "http://api.rrocr.com/api/recognize.html"

    def __init__(self, token: str, referer: str = "https://show.bilibili.com/") -> None:
        self.token = token
        self.referer = referer

    async def solve(self, challenge: GeetestChallenge) -> GeetestResult:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                self.API,
                data={
                    "appkey": self.token,
                    "gt": challenge.gt,
                    "challenge": challenge.challenge,
                    "referer": self.referer,
                },
            )
            data = resp.json()
        if str(data.get("status")) != "0":
            raise RuntimeError(f"第三方打码失败: {data.get('msg')}")
        d = data["data"]
        return GeetestResult(validate=d["validate"], seccode=d.get("seccode", d["validate"] + "|jordan"))


def build_solver(mode: str, rrocr_token: str = "") -> CaptchaSolver:
    """根据配置构建求解器。"""

    if mode == "rrocr" and rrocr_token:
        return RrocrSolver(rrocr_token)
    return ManualSolver()
