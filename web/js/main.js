// 入口：先拉配置（顺带校验 token），再建 WebSocket。
// 其余模块在 import 时注册各自的事件监听。
import { api } from "./api.js";
import { loadConfig } from "./config.js";
import { setUser } from "./login.js";
import { connectWS } from "./ws.js";
import "./monitor.js";
import "./tabs.js";

(async function init() {
  try {
    await loadConfig();
    setUser(await api("/api/login/status"));
    connectWS();
  } catch (e) {
    console.warn("初始化失败:", e);
  }
})();
