"""配置模型与本地 JSON 读写。

配置默认保存在项目根目录的 ``config.json``（可通过环境变量 ``TICKET_BUY_CONFIG`` 覆盖路径）。
结构示例见 ``config.example.json``。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import ipaddress
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

# 写入后限制文件权限，防止其他用户读取凭据
_CONFIG_FILE_MODE = 0o600
ADMIN_TOKEN_ENV = "TICKET_BUY_ADMIN_TOKEN"
_GENERATED_ADMIN_TOKEN = ""

CONFIG_PATH = Path(os.environ.get("TICKET_BUY_CONFIG", "config.json"))


class NotifyConfig(BaseModel):
    """抢中后的推送配置，留空则不推送。"""

    bark_url: str = Field(default="", max_length=2048)
    serverchan_key: str = Field(default="", max_length=128)
    imessage_recipient: str = Field(
        default="",
        max_length=256,
        description="Mac mini 上通过信息 App 发送提醒的手机号或 Apple ID",
    )

    @field_validator("bark_url")
    @classmethod
    def validate_bark_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("Bark URL 必须是 https 地址")
        if parsed.username or parsed.password:
            raise ValueError("Bark URL 不能包含用户名或密码")
        hostname = parsed.hostname.lower()
        if hostname == "localhost" or hostname.endswith(".localhost"):
            raise ValueError("Bark URL 不能指向本机地址")
        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            if (
                ip.is_loopback
                or ip.is_private
                or ip.is_link_local
                or ip.is_unspecified
                or ip.is_multicast
                or ip.is_reserved
            ):
                raise ValueError("Bark URL 不能指向本地或内网地址")
        return value.rstrip("/")

    @field_validator("serverchan_key")
    @classmethod
    def validate_serverchan_key(cls, value: str) -> str:
        if value and not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("Server酱 Key 格式无效")
        return value


class ServerConfig(BaseModel):
    """本地 Web 服务监听配置。"""

    host: str = "127.0.0.1"
    port: int = 8000
    admin_token: str = Field(default="", max_length=512, description="管理 token，可用环境变量覆盖")
    allowed_origins: list[str] = Field(default_factory=list, max_length=20, description="允许跨 Origin 访问的来源")

    @field_validator("allowed_origins")
    @classmethod
    def validate_allowed_origins(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for origin in value:
            if len(origin) > 256:
                raise ValueError("allowed_origins 中的来源过长")
            parsed = urlparse(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("allowed_origins 必须是 http(s) origin")
            if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
                raise ValueError("allowed_origins 只能包含 scheme://host[:port]")
            normalized.append(f"{parsed.scheme}://{parsed.netloc}".rstrip("/"))
        return normalized


class AppConfig(BaseModel):
    """整体配置。"""

    cookie: str = Field(default="", max_length=16384, description="原始 Cookie 字符串，包含 SESSDATA/bili_jct/DedeUserID")

    project_id: int = 0
    screen_id: int = 0
    sku_id: int = 0
    buyer_ids: list[int] = Field(default_factory=list)
    count: int = 1

    start_time: str = Field(default="", max_length=64, description="ISO 时间，如 2026-06-08T20:00:00，留空表示立即开抢")
    interval_ms: int = Field(default=800, ge=100, description="两次下单请求最小间隔（毫秒），启用自适应限速时作为间隔下限")
    max_attempts: int = Field(default=300, ge=1, description="最大尝试次数")
    prewarm_seconds: int = Field(default=30, ge=0, description="开抢前多少秒执行购票人/票价等预热")
    rate_limit_backoff_ms: int = Field(default=2000, ge=1000, description="遇到 429/412 等限流响应时的最小退避（毫秒）")
    rate_limit_cooldown_ms: int = Field(default=8000, ge=1000, description="连续遇到 429/412 后进入冷却的基础等待（毫秒）")
    network_backoff_max_ms: int = Field(default=3000, ge=100, description="网络异常指数退避的最大等待（毫秒）")
    adaptive_rate_enabled: bool = Field(default=True, description="启用 AIMD 自适应限速：遇 429/412 退避，顺畅时逐步加速")
    max_interval_ms: int = Field(default=3000, ge=100, description="自适应限速的间隔上限（毫秒），interval_ms 为下限")
    sold_out_burst_attempts: int = Field(default=6, ge=1, description="启用回流监控时，下单遇库存不足连续冲刺多少次再回监控")
    return_monitor_enabled: bool = Field(default=False, description="是否启用回流票低频监控")
    monitor_interval_ms: int = Field(default=5000, ge=1000, description="回流票监控间隔（毫秒）")
    monitor_end_time: str = Field(default="", max_length=64, description="回流票监控截止时间，ISO 格式")

    captcha_mode: str = Field(default="manual", description="极验处理方式：manual=半自动人工，rrocr=第三方打码")
    rrocr_token: str = Field(default="", max_length=512, description="第三方打码服务 token（captcha_mode=rrocr 时使用）")

    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)


def load_config(path: Path | None = None) -> AppConfig:
    """从磁盘加载配置，不存在则返回默认配置。"""

    target = path or CONFIG_PATH
    if target.exists():
        data = json.loads(target.read_text(encoding="utf-8"))
        return AppConfig.model_validate(data)
    return AppConfig()


def save_config(config: AppConfig, path: Path | None = None) -> None:
    """将配置写回磁盘（UTF-8、缩进美化），并限制文件权限。"""

    target = path or CONFIG_PATH
    target.write_text(
        json.dumps(config.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        target.chmod(_CONFIG_FILE_MODE)
    except OSError:
        pass  # Windows 等不支持 chmod 的平台忽略


def effective_admin_token(config: AppConfig) -> str:
    """返回实际使用的管理 token，环境变量优先。"""

    return os.environ.get(ADMIN_TOKEN_ENV, "") or config.server.admin_token or _GENERATED_ADMIN_TOKEN


def configured_admin_token(config: AppConfig) -> str:
    """返回用户显式配置的管理 token。"""

    return os.environ.get(ADMIN_TOKEN_ENV, "") or config.server.admin_token


def ensure_admin_token(config: AppConfig) -> str:
    """确保当前进程有管理 token。未显式配置时生成临时 token。"""

    global _GENERATED_ADMIN_TOKEN
    token = configured_admin_token(config)
    if token:
        return token
    if not _GENERATED_ADMIN_TOKEN:
        _GENERATED_ADMIN_TOKEN = secrets.token_urlsafe(32)
    return _GENERATED_ADMIN_TOKEN


def using_generated_admin_token(config: AppConfig) -> bool:
    return not configured_admin_token(config) and bool(_GENERATED_ADMIN_TOKEN)


def validate_server_security(config: AppConfig) -> None:
    """校验管理 token 强度。"""

    token = ensure_admin_token(config)
    if len(token) < 16:
        raise RuntimeError("管理 token 至少需要 16 个字符")
