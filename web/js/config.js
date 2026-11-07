import { api, postJSON } from "./api.js";
import { applyDateTime, applyStartTime, collectDateTime, collectStartTime } from "./datetime.js";
import { $ } from "./dom.js";
import {
  currentReturnSkuIds,
  loadProject,
  parseProjectId,
  renderSavedBuyerIds,
  renderSavedReturnSkuIds,
  setSavedSelect,
} from "./project.js";

export function collectConfig() {
  const buyerIds = [...document.querySelectorAll("#buyerList input:checked")].map((c) => parseInt(c.value, 10));
  const returnSkuIds = currentReturnSkuIds();
  const returnMonitorEnabled = $("returnMonitorEnabled").checked;
  return {
    project_id: parseProjectId($("projectInput").value),
    screen_id: parseInt($("screenSelect").value || "0", 10),
    sku_id: parseInt($("skuSelect").value || "0", 10),
    buyer_ids: buyerIds,
    contact_name: $("contactNameInput").value.trim(),
    contact_tel: $("contactTelInput").value.trim(),
    count: parseInt($("countInput").value || "1", 10),
    start_time: collectStartTime(),
    interval_ms: parseInt($("intervalInput").value || "800", 10),
    max_attempts: parseInt($("maxAttemptsInput").value || "300", 10),
    prewarm_seconds: parseInt($("prewarmSecondsInput").value || "30", 10),
    rate_limit_backoff_ms: parseInt($("rateLimitBackoffInput").value || "2000", 10),
    rate_limit_cooldown_ms: parseInt($("rateLimitCooldownInput").value || "8000", 10),
    network_backoff_max_ms: parseInt($("networkBackoffMaxInput").value || "3000", 10),
    adaptive_rate_enabled: $("adaptiveRateEnabled").checked,
    max_interval_ms: parseInt($("maxIntervalInput").value || "3000", 10),
    sold_out_burst_attempts: parseInt($("soldOutBurstInput").value || "6", 10),
    return_monitor_enabled: returnMonitorEnabled,
    return_monitor_sku_ids: returnSkuIds,
    monitor_interval_ms: parseInt($("monitorIntervalInput").value || "5000", 10),
    monitor_end_time: collectDateTime(
      "monitorEndDateInput",
      "monitorEndTimeInput",
      "监控截止时间",
      returnMonitorEnabled,
    ),
    captcha_mode: $("captchaMode").value,
    rrocr_token: $("rrocrToken").value.trim(),
    notify: {
      bark_url: $("barkUrl").value.trim(),
      serverchan_key: $("serverchanKey").value.trim(),
      imessage_recipient: $("imessageRecipient").value.trim(),
    },
  };
}

$("saveConfigBtn").addEventListener("click", async () => {
  try {
    const r = await postJSON("/api/config", collectConfig());
    if (r.ok === false) throw new Error(r.message || "保存失败");
    $("saveHint").textContent = r.ok ? "已保存；到监控页点击“保存并开始抢票”后才会自动等待开抢时间" : (r.message || "保存失败");
  } catch (e) {
    $("saveHint").textContent = e.message || "保存失败";
  }
  setTimeout(() => ($("saveHint").textContent = ""), 2000);
});

export async function loadConfig() {
  const c = await api("/api/config");
  if (c.project_id) $("projectInput").value = c.project_id;
  setSavedSelect("screenSelect", c.screen_id || 0, "场次");
  setSavedSelect("skuSelect", c.sku_id || 0, "票档");
  renderSavedReturnSkuIds(c.return_monitor_sku_ids || []);
  renderSavedBuyerIds(c.buyer_ids || []);
  $("contactNameInput").value = c.contact_name || "";
  $("contactTelInput").value = c.contact_tel || "";
  $("countInput").value = c.count || 1;
  $("intervalInput").value = c.interval_ms || 800;
  $("maxAttemptsInput").value = c.max_attempts || 300;
  $("prewarmSecondsInput").value = c.prewarm_seconds ?? 30;
  $("rateLimitBackoffInput").value = c.rate_limit_backoff_ms ?? 2000;
  $("rateLimitCooldownInput").value = c.rate_limit_cooldown_ms ?? 8000;
  $("networkBackoffMaxInput").value = c.network_backoff_max_ms ?? 3000;
  $("adaptiveRateEnabled").checked = c.adaptive_rate_enabled ?? true;
  $("maxIntervalInput").value = c.max_interval_ms ?? 3000;
  $("soldOutBurstInput").value = c.sold_out_burst_attempts ?? 6;
  $("returnMonitorEnabled").checked = !!c.return_monitor_enabled;
  $("monitorIntervalInput").value = c.monitor_interval_ms || 5000;
  applyDateTime(c.monitor_end_time, "monitorEndDateInput", "monitorEndTimeInput");
  $("captchaMode").value = c.captcha_mode || "manual";
  $("rrocrToken").value = c.rrocr_token || "";
  applyStartTime(c.start_time);
  if (c.notify) {
    $("barkUrl").value = c.notify.bark_url || "";
    $("serverchanKey").value = c.notify.serverchan_key || "";
    $("imessageRecipient").value = c.notify.imessage_recipient || "";
  }
  if (c.project_id) {
    await loadProject(c.project_id, c.screen_id || 0, c.sku_id || 0, c.return_monitor_sku_ids || []);
  }
}
