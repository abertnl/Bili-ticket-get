import { $ } from "./dom.js";

// 单独成模块，避免监控与验证码互相 import 形成环。
export function addLog(ev) {
  const log = $("log");
  const line = document.createElement("div");
  line.className = "log-line " + (ev.level || "info");
  line.textContent = `[${ev.time || ""}] ${ev.message || ""}`;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
  while (log.childElementCount > 500) log.removeChild(log.firstChild);
}
