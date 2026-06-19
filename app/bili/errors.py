"""会员购下单错误码映射与归类。"""

from __future__ import annotations

from enum import Enum

# 常见错误码 -> 人类可读描述
ERROR_MESSAGES: dict[int, str] = {
    0: "成功",
    -101: "账号未登录",
    -111: "csrf 校验失败，请重新登录",
    -400: "请求错误",
    -403: "访问权限不足",
    -404: "无此项目",
    -352: "风控校验失败，需要人机验证",
    412: "请求被风控拦截（412），降低频率后会自动重试",
    100001: "系统繁忙，请重试",    900001: "前方拥堵，请重试",    100009: "库存不足 / 已售罄",
    100016: "项目状态不可售",
    100039: "活动太火爆了，请稍后再试",
    100041: "对未发售的项目下单",
    100048: "已经下单，请勿重复下单",
    100050: "登录态失效，请重新登录",
    100079: "本项目需要联系人/购票人",
    209001: "本项目暂不支持购买",
    429: "请求过于频繁（429），降低频率后会自动重试",
}


class ResultKind(str, Enum):
    """下单结果归类，供状态机决定下一步动作。"""

    SUCCESS = "success"      # 抢中
    RISK = "risk"            # 触发风控，需要走验证码
    RETRY = "retry"          # 可重试（繁忙/库存波动）
    RATE_LIMIT = "rate_limit"  # 请求过频/风控拦截，需要更保守退避
    SOLD_OUT = "sold_out"    # 售罄（仍可继续轮询回流）
    FATAL = "fatal"          # 致命错误，应停止（登录失效/重复下单等）


# 应判定为致命、需要停止抢票的错误码。100048 可能代表已有待支付订单，由状态机单独处理。
_FATAL_CODES = {-101, -111, 100050}
# 售罄类
_SOLD_OUT_CODES = {100009}
# 风控
_RISK_CODES = {-352}
# 限流/风控拦截类
_RATE_LIMIT_CODES = {412, 429}


def describe(code: int) -> str:
    """返回错误码的可读描述。"""

    return ERROR_MESSAGES.get(code, f"未知错误码 {code}")


def classify(code: int) -> ResultKind:
    """把错误码归类为状态机可处理的类别。"""

    if code == 0:
        return ResultKind.SUCCESS
    if code in _RISK_CODES:
        return ResultKind.RISK
    if code in _RATE_LIMIT_CODES:
        return ResultKind.RATE_LIMIT
    if code in _FATAL_CODES:
        return ResultKind.FATAL
    if code in _SOLD_OUT_CODES:
        return ResultKind.SOLD_OUT
    # 其余（繁忙、9000xx 等）默认重试
    return ResultKind.RETRY
