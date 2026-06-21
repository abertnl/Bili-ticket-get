"""会员购票务接口：演出信息、购票人、预下单、下单、订单状态。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .client import BiliClient

BASE = "https://show.bilibili.com"
MALL_BASE = "https://mall.bilibili.com"
PROJECT_NEW_URL = f"{MALL_BASE}/mall-search-items/items_detail/info"
PROJECT_URL = f"{BASE}/api/ticket/project/getV2"
BUYER_LIST_URL = f"{BASE}/api/ticket/buyer/list"
PREPARE_URL = f"{BASE}/api/ticket/order/prepare"
CREATE_URL = f"{BASE}/api/ticket/order/createV2"
ORDER_INFO_URL = f"{BASE}/api/ticket/order/info"
PAY_PARAM_URL = f"{BASE}/api/ticket/order/getPayParam"
ORDER_DETAIL_URL = f"{BASE}/platform/orderDetail.html"


@dataclass
class TicketSku:
    """票档（座位/价格档位）。"""

    sku_id: int
    desc: str
    price: int          # 单位：分
    sale_flag: str      # 售卖状态文案
    num: int            # 库存提示（部分项目为 0）


@dataclass
class Screen:
    """场次。"""

    screen_id: int
    name: str
    skus: list[TicketSku] = field(default_factory=list)


@dataclass
class Project:
    """演出项目。"""

    project_id: int
    name: str
    screens: list[Screen] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Buyer:
    """购票人。"""

    buyer_id: int
    name: str
    tel: str
    id_card: str  # 已脱敏的证件号文案


@dataclass
class PrepareResult:
    """order/prepare 返回结果。"""

    code: int
    message: str
    token: str = ""
    ptoken: str = ""
    v_voucher: str = ""           # 触发风控时的凭据
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CreateResult:
    """createV2 返回结果。"""

    code: int
    message: str
    order_id: str = ""
    pay_token: str = ""
    pay_money: int = 0
    v_voucher: str = ""           # 触发风控时的凭据
    raw: dict[str, Any] = field(default_factory=dict)


def build_buyer_info(buyers: list[Buyer]) -> list[dict[str, Any]]:
    """构造会员购下单/预下单需要的购票人结构。"""

    return [
        {
            "id": b.buyer_id,
            "name": b.name,
            "tel": b.tel,
            "personal_id": b.id_card,
            "isBuyerInfoVerified": True,
            "isBuyerValid": True,
        }
        for b in buyers
    ]


def build_contact_info(
    buyers: list[Buyer],
    contact_name: str = "",
    contact_tel: str = "",
) -> dict[str, str]:
    """构造部分项目要求的顶层联系人字段。"""

    fallback = buyers[0] if buyers else None
    name = (contact_name or (fallback.name if fallback else "")).strip()
    tel = (contact_tel or (fallback.tel if fallback else "")).strip()
    if not name or not tel:
        return {}
    return {"buyer": name, "tel": tel}


def _dict_data(data: dict[str, Any]) -> dict[str, Any]:
    inner = data.get("data", {})
    return inner if isinstance(inner, dict) else {}


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


SALE_FLAG_NUMBER_MAP: dict[int, str] = {
    1: "不可售",
    2: "预售",
    3: "停售",
    4: "售罄",
    5: "不可用",
    6: "库存紧张",
    8: "暂时售罄",
    9: "不在白名单",
    101: "未开始",
    102: "已结束",
    103: "未完成",
    105: "下架",
    106: "已取消",
}


def _sale_flag_text(raw: Any, fallback_number: Any = None) -> str:
    if isinstance(raw, dict):
        for key in ("display_name", "name", "status_name", "desc"):
            value = raw.get(key)
            if value:
                return str(value)
        fallback_number = raw.get("number", fallback_number)
    elif isinstance(raw, int):
        fallback_number = raw
    elif raw not in (None, ""):
        return str(raw)

    number = _int_or_zero(fallback_number)
    return SALE_FLAG_NUMBER_MAP.get(number, "")


def _parse_sku(raw: dict[str, Any]) -> TicketSku:
    return TicketSku(
        sku_id=_int_or_zero(raw.get("id") or raw.get("sku_id") or raw.get("skuId")),
        desc=str(raw.get("desc", "") or raw.get("name", "") or raw.get("screenName", "")),
        price=_int_or_zero(raw.get("price") or raw.get("originPrice")),
        sale_flag=_sale_flag_text(
            raw.get("sale_flag") or raw.get("saleFlag"),
            raw.get("sale_flag_number") or raw.get("saleFlagNumber"),
        ),
        num=_int_or_zero(raw.get("num") or raw.get("stock") or raw.get("stockNum")),
    )


def _project_from_payload(project_id: int, data: dict[str, Any]) -> Project:
    screens: list[Screen] = []
    for s in data.get("screen_list", []):
        if not isinstance(s, dict):
            continue
        skus = [_parse_sku(t) for t in s.get("ticket_list", []) if isinstance(t, dict)]
        screens.append(
            Screen(
                screen_id=_int_or_zero(s.get("id") or s.get("screen_id") or s.get("screenId")),
                name=str(s.get("name", "")),
                skus=skus,
            )
        )
    return Project(
        project_id=_int_or_zero(data.get("id") or data.get("project_id") or project_id),
        name=str(data.get("name", "") or data.get("projectName", "")),
        screens=screens,
        raw=data,
    )


def _normalize_new_project_payload(data: dict[str, Any], project_id: int) -> dict[str, Any]:
    normalized_project_id = _int_or_zero(data.get("projectId") or data.get("itemsId") or project_id)
    screens = data.get("screenList", [])
    if not isinstance(screens, list) or not screens:
        raise RuntimeError("新版演出信息缺少场次列表")

    normalized_screens: list[dict[str, Any]] = []
    for screen in screens:
        if not isinstance(screen, dict):
            continue
        screen_copy = dict(screen)
        screen_copy.setdefault("project_id", normalized_project_id)
        tickets: list[dict[str, Any]] = []
        ticket_list = screen_copy.get("ticket_list") or screen_copy.get("ticketList") or []
        for ticket_item in ticket_list:
            if not isinstance(ticket_item, dict):
                continue
            ticket_copy = dict(ticket_item)
            ticket_copy.setdefault("project_id", normalized_project_id)
            ticket_copy.setdefault("screen_name", screen_copy.get("name", ""))
            sale_flag = ticket_copy.get("sale_flag") or ticket_copy.get("saleFlag") or {}
            if isinstance(sale_flag, dict):
                ticket_copy.setdefault("sale_flag_number", sale_flag.get("number"))
            tickets.append(ticket_copy)
        screen_copy["ticket_list"] = tickets
        normalized_screens.append(screen_copy)

    return {
        "id": normalized_project_id,
        "name": data.get("projectName", ""),
        "hotProject": bool(data.get("hotProject", False)),
        "screen_list": normalized_screens,
        "sales_dates": data.get("salesDates", []) if isinstance(data.get("salesDates"), list) else [],
        "start_time": data.get("startTime") or data.get("start_time") or 0,
        "end_time": data.get("endTime") or 0,
    }


async def _get_project_new(client: BiliClient, project_id: int) -> Project:
    data = await client.post_json(
        PROJECT_NEW_URL,
        json={"itemsId": project_id, "itemsDetailPageType": 3},
        headers={
            "Origin": MALL_BASE,
            "Referer": (
                f"{MALL_BASE}/neul-next/ticket-renovation/detail.html"
                f"?id={project_id}&from=pc_ticketlist&noTitleBar=1"
            ),
        },
    )
    code = data.get("code", data.get("errno"))
    if data.get("success") is False or code not in (None, 0):
        raise RuntimeError(data.get("message") or data.get("msg") or "新版演出信息加载失败")
    inner = data.get("data")
    if not isinstance(inner, dict):
        raise RuntimeError("新版演出信息为空")
    return _project_from_payload(project_id, _normalize_new_project_payload(inner, project_id))


async def _get_project_old(client: BiliClient, project_id: int) -> Project:
    data = await client.get_json(PROJECT_URL, params={"version": 134, "id": project_id, "project_id": project_id})
    if data.get("code", data.get("errno", 0)) != 0:
        raise RuntimeError(f"获取演出信息失败: {data.get('msg') or data.get('message')}")
    d = data["data"]
    return _project_from_payload(project_id, d)


async def get_project(client: BiliClient, project_id: int) -> Project:
    """拉取演出信息（场次 + 票档）。"""

    try:
        return await _get_project_new(client, project_id)
    except Exception as new_error:  # noqa: BLE001
        try:
            return await _get_project_old(client, project_id)
        except Exception as old_error:  # noqa: BLE001
            raise RuntimeError(
                "获取演出信息失败: "
                f"新版接口={new_error}; 旧版接口={old_error}"
            ) from old_error


async def get_buyers(client: BiliClient) -> list[Buyer]:
    """拉取已保存的购票人列表。"""

    data = await client.get_json(BUYER_LIST_URL)
    # 会员购部分接口返回 errno/errtag/msg，而不是常见的 code/message。
    # buyer/list 成功响应形如 {"errno": 0, "msg": "", "data": {"list": [...]}}。
    code = data.get("code", data.get("errno", 0))
    if code != 0:
        raise RuntimeError(f"获取购票人失败: {data.get('msg') or data.get('message') or code}")
    result: list[Buyer] = []
    for b in data.get("data", {}).get("list", []):
        result.append(
            Buyer(
                buyer_id=b.get("id", 0),
                name=b.get("name", ""),
                tel=b.get("tel", ""),
                id_card=b.get("personal_id", ""),
            )
        )
    return result


async def prepare_order(
    client: BiliClient,
    project_id: int,
    screen_id: int,
    sku_id: int,
    count: int,
    buyers: list[Buyer],
) -> PrepareResult:
    """预下单，返回结构化结果（含 token 与风控凭据）。"""

    payload = {
        "project_id": project_id,
        "screen_id": screen_id,
        "order_type": 1,
        "count": count,
        "sku_id": sku_id,
        "buyer_info": build_buyer_info(buyers),
        "ignoreRequestLimit": True,
        "ticket_agent": "",
        "token": "",
        "requestSource": "neul-next",
        "newRisk": True,
        "csrf": client.csrf,
    }
    resp = await client.post(PREPARE_URL, params={"project_id": project_id}, json=payload)
    try:
        data = resp.json()
    except ValueError:
        return PrepareResult(code=resp.status_code, message=f"HTTP {resp.status_code}", raw={})

    inner = _dict_data(data)
    return PrepareResult(
        code=data.get("code", data.get("errno", resp.status_code)),
        message=data.get("msg") or data.get("message", ""),
        token=inner.get("token", ""),
        ptoken=inner.get("ptoken", ""),
        v_voucher=inner.get("v_voucher", "") or resp.headers.get("x-bili-gaia-vvoucher", ""),
        raw=data,
    )


def normalize_prepare_ptoken(value: str | None) -> str:
    if value is None:
        return ""
    return str(value).replace("=", "")


async def create_order(
    client: BiliClient,
    project_id: int,
    screen_id: int,
    sku_id: int,
    count: int,
    token: str,
    buyers: list[Buyer],
    pay_money: int,
    extra_params: dict[str, Any] | None = None,
    ptoken: str = "",
    contact_name: str = "",
    contact_tel: str = "",
) -> CreateResult:
    """提交订单（核心抢票请求）。

    ``extra_params`` 用于在风控恢复时追加 ``gaia_vtoken`` 等 URL 参数。
    """

    buyer_info = build_buyer_info(buyers)
    contact_info = build_contact_info(buyers, contact_name, contact_tel)
    now_ms = int(time.time() * 1000)
    normalized_ptoken = normalize_prepare_ptoken(ptoken)
    body = {
        "project_id": project_id,
        "screen_id": screen_id,
        "sku_id": sku_id,
        "count": count,
        "order_type": 1,
        "pay_money": pay_money * count if pay_money else 0,
        "again": 1,
        "token": token,
        "timestamp": now_ms,
        "newRisk": True,
        "requestSource": "neul-next",
        "orderCreateUrl": CREATE_URL,
        "deviceId": "",
        "buyer_info": json.dumps(buyer_info, ensure_ascii=False),
        "csrf": client.csrf,
    }
    body.update(contact_info)
    params: dict[str, Any] = {"project_id": project_id}
    if normalized_ptoken:
        body["ptoken"] = normalized_ptoken
        params["ptoken"] = normalized_ptoken
    if extra_params:
        params.update(extra_params)

    resp = await client.post(CREATE_URL, params=params, json=body)
    try:
        data = resp.json()
    except ValueError:
        # 例如 429 直接返回非 JSON
        return CreateResult(code=resp.status_code, message=f"HTTP {resp.status_code}", raw={})

    inner = _dict_data(data)
    return CreateResult(
        code=data.get("code", data.get("errno", resp.status_code)),
        message=data.get("msg") or data.get("message", ""),
        order_id=str(inner.get("orderId", "") or inner.get("order_id", "")),
        pay_token=inner.get("token", ""),
        pay_money=_int_or_zero(inner.get("pay_money") or inner.get("payMoney") or data.get("pay_money")),
        v_voucher=inner.get("v_voucher", "") or resp.headers.get("x-bili-gaia-vvoucher", ""),
        raw=data,
    )


def get_order_detail_url(order_id: int | str) -> str:
    return f"{ORDER_DETAIL_URL}?order_id={order_id}"


async def get_pay_qrcode_url(client: BiliClient, order_id: int | str) -> str:
    data = await client.get_json(PAY_PARAM_URL, params={"order_id": order_id})
    code = data.get("code", data.get("errno", -1))
    if code != 0:
        return ""
    return str(data.get("data", {}).get("code_url", "") or "")
