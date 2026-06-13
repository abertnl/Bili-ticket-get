"""配置模型与本地 JSON 读写。

配置默认保存在项目根目录的 ``config.json``（可通过环境变量 ``TICKET_BUY_CONFIG`` 覆盖路径）。
结构示例见 ``config.example.json``。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field

# 写入后限制文件权限，防止其他用户读取凭据
_CONFIG_FILE_MODE = 0o600

CONFIG_PATH = Path(os.environ.get("TICKET_BUY_CONFIG", "config.json"))


class NotifyConfig(BaseModel):
    """抢中后的推送配置，留空则不推送。"""

    bark_url: str = ""
    serverchan_key: str = ""
    imessage_recipient: str = Field(default="", description="Mac mini 上通过信息 App 发送提醒的手机号或 Apple ID")


class ServerConfig(BaseModel):
    """本地 Web 服务监听配置。"""

    host: str = "127.0.0.1"
    port: int = 8000


class AppConfig(BaseModel):
    """整体配置。"""

    cookie: str = Field(default="", description="原始 Cookie 字符串，包含 SESSDATA/bili_jct/DedeUserID")

    project_id: int = 0
    screen_id: int = 0
    sku_id: int = 0
    buyer_ids: list[int] = Field(default_factory=list)
    count: int = 1

    start_time: str = Field(default="", description="ISO 时间，如 2026-06-08T20:00:00，留空表示立即开抢")
    interval_ms: int = Field(default=800, ge=100, description="两次下单请求最小间隔（毫秒）")
    max_attempts: int = Field(default=300, ge=1, description="最大尝试次数")
    prewarm_seconds: int = Field(default=30, ge=0, description="开抢前多少秒执行购票人/票价等预热")
    rate_limit_backoff_ms: int = Field(default=2000, ge=1000, description="遇到 429/412 等限流响应时的最小退避（毫秒）")
    network_backoff_max_ms: int = Field(default=3000, ge=100, description="网络异常指数退避的最大等待（毫秒）")
    return_monitor_enabled: bool = Field(default=False, description="是否启用回流票低频监控")
    monitor_interval_ms: int = Field(default=5000, ge=1000, description="回流票监控间隔（毫秒）")
    monitor_end_time: str = Field(default="", description="回流票监控截止时间，ISO 格式")

    captcha_mode: str = Field(default="manual", description="极验处理方式：manual=半自动人工，rrocr=第三方打码")
    rrocr_token: str = Field(default="", description="第三方打码服务 token（captcha_mode=rrocr 时使用）")

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
