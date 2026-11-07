"""前端静态资源回归测试。"""

import unittest

from app import server


class FrontendAuthFlowTests(unittest.TestCase):
    def test_frontend_waits_for_config_before_opening_websocket(self) -> None:
        source = (server.WEB_DIR / "js" / "main.js").read_text(encoding="utf-8")
        init_start = source.index("(async function init()")
        init_source = source[init_start:]

        self.assertLess(init_source.index("await loadConfig();"), init_source.index("connectWS();"))

    def test_websocket_reconnect_checks_auth_before_retrying(self) -> None:
        source = (server.WEB_DIR / "js" / "ws.js").read_text(encoding="utf-8")

        self.assertNotIn("ws.onclose = () => setTimeout(connectWS, 2000);", source)

    def test_index_loads_the_module_entrypoint(self) -> None:
        source = (server.WEB_DIR / "index.html").read_text(encoding="utf-8")

        self.assertIn('<script type="module" src="/js/main.js">', source)
        self.assertNotIn('src="/app.js"', source)
