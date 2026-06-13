"""抢中推送：Bark / Server酱 / macOS 信息。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from urllib.parse import quote

import httpx

from .config import NotifyConfig


async def _send_bark(base_url: str, title: str, body: str) -> None:
    """Bark 推送。base_url 形如 https://api.day.app/your_key"""

    url = f"{base_url.rstrip('/')}/{quote(title)}/{quote(body)}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.get(url)


async def _send_serverchan(key: str, title: str, body: str) -> None:
    """Server酱 (sct) 推送。"""

    url = f"https://sctapi.ftqq.com/{key}.send"
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(url, data={"title": title, "desp": body})


async def _send_imessage(recipient: str, title: str, body: str) -> None:
    """macOS 信息 App 推送。recipient 可填手机号或 Apple ID。"""

    script = """
on run argv
    set targetRecipient to item 1 of argv
    set messageText to item 2 of argv
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy targetRecipient of targetService
        send messageText to targetBuddy
    end tell
end run
"""
    proc = await asyncio.create_subprocess_exec(
        "osascript",
        "-e",
        script,
        recipient,
        f"{title}\n{body}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
    if proc.returncode != 0:
        error = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(error or f"osascript exited with {proc.returncode}")


async def send_all(config: NotifyConfig, title: str, body: str) -> None:
    """根据配置把消息推送到所有已配置渠道。"""

    async def capture(name: str, action: Awaitable[None]) -> str | None:
        try:
            await action
        except Exception as exc:  # noqa: BLE001
            return f"{name}: {exc}"
        return None

    tasks: list[Awaitable[str | None]] = []
    if config.bark_url:
        tasks.append(capture("Bark", _send_bark(config.bark_url, title, body)))
    if config.serverchan_key:
        tasks.append(capture("Server酱", _send_serverchan(config.serverchan_key, title, body)))
    if config.imessage_recipient:
        tasks.append(capture("iMessage", _send_imessage(config.imessage_recipient, title, body)))
    if not tasks:
        return
    errors = [error for error in await asyncio.gather(*tasks) if error]
    if errors:
        raise RuntimeError("; ".join(errors))
