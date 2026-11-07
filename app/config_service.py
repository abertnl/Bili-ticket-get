"""配置公开表示、局部更新及任务启动前校验。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .api_schemas import SENSITIVE_PLACEHOLDER
from .config import AppConfig
from .time_utils import parse_local_datetime


def public_config(config: AppConfig) -> dict[str, Any]:
    """返回给浏览器的配置，避免回显账号凭据和推送密钥。"""

    data = config.model_dump(exclude={"cookie", "rrocr_token"})
    data["server"].pop("admin_token", None)
    data["has_cookie"] = bool(config.cookie)
    data["has_rrocr_token"] = bool(config.rrocr_token)
    data["admin_token_required"] = True
    data["notify"] = {
        "bark_url": SENSITIVE_PLACEHOLDER if config.notify.bark_url else "",
        "serverchan_key": SENSITIVE_PLACEHOLDER if config.notify.serverchan_key else "",
        "imessage_recipient": SENSITIVE_PLACEHOLDER if config.notify.imessage_recipient else "",
    }
    data["notify_configured"] = {
        "bark_url": bool(config.notify.bark_url),
        "serverchan_key": bool(config.notify.serverchan_key),
        "imessage_recipient": bool(config.notify.imessage_recipient),
    }
    return data


def merge_sensitive_updates(config: AppConfig, updates: dict[str, Any]) -> dict[str, Any]:
    """敏感字段占位符表示保留旧值，空字符串表示清空。"""

    updates = updates.copy()
    if updates.get("rrocr_token") == "":
        updates.pop("rrocr_token")
    notify = updates.get("notify")
    if isinstance(notify, dict):
        current = config.notify.model_dump()
        merged_notify = current.copy()
        for key, value in notify.items():
            if value == SENSITIVE_PLACEHOLDER:
                merged_notify[key] = current.get(key, "")
            else:
                merged_notify[key] = value
        updates["notify"] = merged_notify
    return updates


def _parse_local_datetime(value: str, label: str) -> datetime | None:
    try:
        return parse_local_datetime(value)
    except ValueError as exc:
        raise ValueError(f"{label}格式无效") from exc


def validate_grab_config(config: AppConfig) -> str | None:
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
