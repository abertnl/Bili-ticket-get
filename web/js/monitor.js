import { postJSON } from "./api.js";
import { checkCaptcha } from "./captcha.js";
import { collectConfig } from "./config.js";
import { $ } from "./dom.js";

export function applyStatus(s) {
  $("statAttempts").textContent = s.attempts ?? 0;
  $("statMonitorChecks").textContent = s.monitor_checks ?? 0;
  $("statCode").textContent = s.last_code ?? "-";
  $("statState").textContent = s.success ? "成功" : s.running ? "抢票中" : (s.finished_reason || "空闲");
  $("statLastAttemptMs").textContent = `${s.last_attempt_ms ?? 0}ms`;
  $("statAvgAttemptMs").textContent = `${s.avg_attempt_ms ?? 0}ms`;
  $("statNetworkErrors").textContent = s.network_errors ?? 0;
  $("statRateLimit").textContent = s.rate_limit_count ?? 0;
  $("statCongestion").textContent = s.congestion_count ?? 0;
  $("statSoldOut").textContent = s.sold_out_count ?? 0;
  $("statDynamicInterval").textContent = `${s.dynamic_interval_ms ?? 0}ms`;
  $("statEffectiveOrders").textContent = s.effective_order_attempts ?? 0;
  $("statTargetSku").textContent = s.target_sku_desc
    ? `${s.target_sku_desc}(${s.target_sku_id || "-"})`
    : "-";
  $("statMonitorTargets").textContent = s.monitor_target_count ?? 0;
  $("statPhase").textContent = s.phase || "idle";
  $("statTimeOffset").textContent = `${s.time_offset_ms ?? 0}ms`;
  $("statPrewarm").textContent = s.prewarm_ok ? "成功" : "-";
  $("statTransport").textContent = s.transport || "-";
  $("statTelemetryPath").textContent = s.telemetry_path || "-";
  $("statTelemetryPath").title = s.telemetry_path || "";
  $("retryInfo").textContent = s.running && s.retry_reason
    ? `下次重试：${s.retry_reason} · 等待 ${s.retry_delay_ms ?? 0}ms`
    : "";
  const detail = [s.last_message, s.last_stock_status].filter(Boolean).join(" · ");
  $("lastMsg").textContent = detail;
  $("statState").classList.toggle("success", !!s.success);
  $("lastMsg").classList.toggle("success", !!s.success);
  const hasPayment = !!s.payment_url;
  $("paymentBox").classList.toggle("hidden", !hasPayment);
  $("orderIdText").textContent = s.order_id ? `订单 ${s.order_id}` : "";
  if (hasPayment) {
    $("paymentUrl").href = s.payment_url;
  }
  const hasQr = !!s.pay_qrcode_url;
  $("payQrUrl").classList.toggle("hidden", !hasQr);
  if (hasQr) {
    $("payQrUrl").href = s.pay_qrcode_url;
  }
  if (s.waiting_captcha) checkCaptcha();
}

$("startBtn").addEventListener("click", async () => {
  try {
    const saved = await postJSON("/api/config", collectConfig());
    if (saved.ok === false) throw new Error(saved.message || "保存配置失败");
    const r = await postJSON("/api/grab/start", {});
    if (!r.ok) alert(r.message || "启动失败");
  } catch (e) {
    alert(e.message || "启动失败");
  }
});

$("stopBtn").addEventListener("click", async () => {
  try {
    await postJSON("/api/grab/stop", {});
  } catch (e) {
    alert(e.message || "停止失败");
  }
});
