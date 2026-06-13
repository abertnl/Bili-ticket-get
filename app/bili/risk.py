"""风控处理：gaia-vgate register/validate，取得 gaia_vtoken。

触发 ``-352`` 后流程：
1. ``register``（传 v_voucher）-> 得到极验 gt/challenge 与 token；
2. 调 ``CaptchaSolver`` 完成极验 -> validate/seccode；
3. ``validate`` -> 得到 grisk_id，即 gaia_vtoken；
4. 把 grisk_id 写入 Cookie ``x-bili-gaia-vtoken``，并作为原接口 URL 参数 ``gaia_vtoken`` 重发。
"""

from __future__ import annotations

from .captcha import CaptchaSolver, GeetestChallenge
from .client import BiliClient

REGISTER_URL = "https://api.bilibili.com/x/gaia-vgate/v1/register"
VALIDATE_URL = "https://api.bilibili.com/x/gaia-vgate/v1/validate"


class RiskError(RuntimeError):
    """风控处理失败。"""


class RiskHandler:
    """负责一次完整的风控解除流程。"""

    def __init__(self, solver: CaptchaSolver) -> None:
        self.solver = solver

    async def _register(self, client: BiliClient, v_voucher: str) -> dict:
        data = await client.post_json(
            REGISTER_URL,
            params={"csrf": client.csrf, "v_voucher": v_voucher},
        )
        if data.get("code") != 0:
            raise RiskError(f"register 失败: {data.get('message')}")
        return data["data"]

    async def _validate(
        self, client: BiliClient, token: str, challenge: str, validate: str, seccode: str
    ) -> str:
        data = await client.post_json(
            VALIDATE_URL,
            params={
                "csrf": client.csrf,
                "token": token,
                "challenge": challenge,
                "validate": validate,
                "seccode": seccode,
            },
        )
        if data.get("code") != 0:
            raise RiskError(f"validate 失败: {data.get('message')}")
        inner = data["data"]
        if not inner.get("is_valid"):
            raise RiskError("validate 未通过")
        return inner["grisk_id"]

    async def handle(self, client: BiliClient, v_voucher: str) -> str:
        """执行完整风控流程，返回 gaia_vtoken（grisk_id）。"""

        reg = await self._register(client, v_voucher)
        if reg.get("type") != "geetest" or not reg.get("geetest"):
            raise RiskError("该风控无法通过极验解除（可能需要更换 IP/设备）")
        gt = reg["geetest"]["gt"]
        challenge = reg["geetest"]["challenge"]
        token = reg["token"]

        result = await self.solver.solve(GeetestChallenge(gt=gt, challenge=challenge))
        grisk_id = await self._validate(
            client, token, challenge, result.validate, result.seccode
        )
        # 写入 cookie，供后续请求恢复访问
        client.set_cookie("x-bili-gaia-vtoken", grisk_id)
        return grisk_id
