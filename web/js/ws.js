import { isAuthRedirecting, redirectToAuth } from "./api.js";
import { addLog } from "./log.js";
import { applyStatus } from "./monitor.js";

let ws = null;
let wsReconnectTimer = null;

export function connectWS() {
  if (isAuthRedirecting() || ws) return;
  let opened = false;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    opened = true;
  };
  ws.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "log") addLog(ev);
    else if (ev.type === "status") applyStatus(ev);
  };
  ws.onclose = (event) => {
    ws = null;
    // 从未握手成功或被服务端以 1008 拒绝，都说明 token 失效，回登录页。
    if (isAuthRedirecting() || event.code === 1008 || !opened) {
      redirectToAuth();
      return;
    }
    if (wsReconnectTimer) clearTimeout(wsReconnectTimer);
    wsReconnectTimer = setTimeout(connectWS, 2000);
  };
}
