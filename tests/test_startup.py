"""启动输出回归测试。"""

import unittest
from unittest.mock import patch

from app.config import AppConfig, ServerConfig
from tests.helpers import load_entrypoint_module


class StartupOutputTests(unittest.TestCase):
    def test_configured_admin_token_is_printed_on_startup(self) -> None:
        entrypoint = load_entrypoint_module()
        config = AppConfig(server=ServerConfig(admin_token="configured-token-123"))
        lines: list[str] = []

        with (
            patch.object(entrypoint, "load_config", return_value=config),
            patch.object(entrypoint.uvicorn, "run"),
            patch("builtins.print", side_effect=lines.append),
        ):
            entrypoint.main()

        self.assertIn("管理 token：configured-token-123", lines)
        self.assertIn("token 来源：config.json 的 server.admin_token", lines)
