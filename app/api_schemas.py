"""浏览器 API 的请求模型与可更新字段白名单。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .config import NotifyConfig

SENSITIVE_PLACEHOLDER = "***"


class CookieLogin(BaseModel):
    cookie: str = Field(max_length=16384)


class CaptchaSubmit(BaseModel):
    validate_value: str = Field(alias="validate", max_length=2048)
    seccode: str = Field(max_length=2048)


class NotifyUpdate(BaseModel):
    bark_url: str = Field(default="", max_length=2048)
    serverchan_key: str = Field(default="", max_length=128)
    imessage_recipient: str = Field(default="", max_length=256)

    @field_validator("bark_url")
    @classmethod
    def validate_bark_url(cls, value: str) -> str:
        if value == SENSITIVE_PLACEHOLDER:
            return value
        return NotifyConfig.validate_bark_url(value)

    @field_validator("serverchan_key")
    @classmethod
    def validate_serverchan_key(cls, value: str) -> str:
        if value == SENSITIVE_PLACEHOLDER:
            return value
        return NotifyConfig.validate_serverchan_key(value)


class ConfigUpdate(BaseModel):
    """允许通过配置接口修改的字段白名单。"""

    project_id: int | None = Field(default=None, ge=0)
    screen_id: int | None = Field(default=None, ge=0)
    sku_id: int | None = Field(default=None, ge=0)
    buyer_ids: list[int] | None = None
    contact_name: str | None = Field(default=None, max_length=64)
    contact_tel: str | None = Field(default=None, max_length=32)
    count: int | None = Field(default=None, ge=1)
    start_time: str | None = Field(default=None, max_length=64)
    interval_ms: int | None = Field(default=None, ge=100)
    max_attempts: int | None = Field(default=None, ge=1)
    prewarm_seconds: int | None = Field(default=None, ge=0)
    rate_limit_backoff_ms: int | None = Field(default=None, ge=1000)
    rate_limit_cooldown_ms: int | None = Field(default=None, ge=1000)
    network_backoff_max_ms: int | None = Field(default=None, ge=100)
    adaptive_rate_enabled: bool | None = None
    max_interval_ms: int | None = Field(default=None, ge=100)
    time_sync_enabled: bool | None = None
    connection_prewarm_enabled: bool | None = None
    http2_enabled: bool | None = None
    payment_link_enabled: bool | None = None
    sold_out_burst_attempts: int | None = Field(default=None, ge=1)
    return_monitor_enabled: bool | None = None
    return_monitor_sku_ids: list[int] | None = None
    monitor_interval_ms: int | None = Field(default=None, ge=1000)
    monitor_end_time: str | None = Field(default=None, max_length=64)
    captcha_mode: Literal["manual", "rrocr"] | None = None
    rrocr_token: str | None = Field(default=None, max_length=512)
    notify: NotifyUpdate | None = None

    @field_validator("buyer_ids")
    @classmethod
    def validate_buyer_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and any(buyer_id <= 0 for buyer_id in value):
            raise ValueError("buyer_ids 必须是正整数")
        return value

    @field_validator("return_monitor_sku_ids")
    @classmethod
    def validate_return_monitor_sku_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and any(sku_id <= 0 for sku_id in value):
            raise ValueError("return_monitor_sku_ids 必须是正整数")
        return value
