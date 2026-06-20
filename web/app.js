"use strict";

const $ = (id) => document.getElementById(id);
let authRedirecting = false;
function redirectToAuth() {
  if (authRedirecting) return;
  authRedirecting = true;
  location.replace("/");
}

const api = async (url, opts) => {
  const response = await fetch(url, opts);
  const text = await response.text();
  let data = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (_) {
      data = { message: text };
    }
  }
  if (!response.ok) {
    data.ok = false;
    data.status = response.status;
    data.message = data.message || data.detail || `请求失败：HTTP ${response.status}`;
    if (response.status === 401 && data.message.startsWith("未授权：")) {
      redirectToAuth();
      throw new Error(data.message);
    }
  }
  return data;
};
const postJSON = (url, body) =>
  api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

// ---------- Tab 切换 ----------
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $("tab-" + tab.dataset.tab).classList.add("active");
  });
});

// ---------- 登录 ----------
let pollTimer = null;
function setUser(info) {
  if (info && info.is_login) {
    $("userChip").textContent = "已登录：" + (info.uname || info.mid || "");
    $("userChip").classList.add("ok");
  } else {
    $("userChip").textContent = "未登录";
    $("userChip").classList.remove("ok");
  }
}

$("genQrBtn").addEventListener("click", async () => {
  $("qrStatus").textContent = "正在生成…";
  try {
    const data = await api("/api/login/qr");
    if (!data.qrcode_key || !data.image) throw new Error(data.message || "生成二维码失败");
    $("qrImg").src = data.image;
    $("qrStatus").textContent = "请使用 B 站 App 扫码";
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      try {
        const r = await api("/api/login/poll?qrcode_key=" + encodeURIComponent(data.qrcode_key));
        if (r.ok === false) throw new Error(r.message || "轮询扫码状态失败");
        if (r.code === 0) {
          clearInterval(pollTimer);
          $("qrStatus").textContent = "登录成功！";
          setUser(r.user || { is_login: true });
        } else if (r.code === 86038) {
          clearInterval(pollTimer);
          $("qrStatus").textContent = "二维码已失效，请重新生成";
        } else if (r.code === 86090) {
          $("qrStatus").textContent = "已扫码，请在手机上确认";
        } else {
          $("qrStatus").textContent = "等待扫码…";
        }
      } catch (e) {
        clearInterval(pollTimer);
        $("qrStatus").textContent = e.message || "轮询扫码状态失败";
      }
    }, 2000);
  } catch (e) {
    $("qrStatus").textContent = e.message || "生成二维码失败";
  }
});

$("cookieBtn").addEventListener("click", async () => {
  const cookie = $("cookieInput").value.trim();
  if (!cookie) return alert("请粘贴 Cookie");
  try {
    const r = await postJSON("/api/login/cookie", { cookie });
    if (r.ok) {
      setUser(r.user);
      alert("登录成功：" + (r.user.uname || ""));
    } else {
      alert(r.message || "登录失败");
    }
  } catch (e) {
    alert(e.message || "登录失败");
  }
});

// ---------- 配置 ----------
function parseProjectId(text) {
  text = (text || "").trim();
  const m = text.match(/(?:id=|project_id=|\/detail\/)(\d+)/);
  if (m) return parseInt(m[1], 10);
  if (/^\d+$/.test(text)) return parseInt(text, 10);
  return 0;
}

function collectStartTime() {
  const date = $("startDateInput").value;
  const time = $("startTimeInput").value;
  if (!date && !time) return "";
  if (!date || !time) throw new Error("请选择完整的开抢日期和时间");
  return `${date}T${time.length === 5 ? `${time}:00` : time}`;
}

function applyStartTime(value) {
  if (!value) {
    $("startDateInput").value = "";
    $("startTimeInput").value = "";
    return;
  }
  const [date = "", time = ""] = value.split("T");
  $("startDateInput").value = date;
  $("startTimeInput").value = time.slice(0, 8);
}

function collectDateTime(dateId, timeId, label, required = false) {
  const date = $(dateId).value;
  const time = $(timeId).value;
  if (!date && !time) {
    if (required) throw new Error(`请选择${label}`);
    return "";
  }
  if (!date || !time) throw new Error(`请选择完整的${label}`);
  return `${date}T${time.length === 5 ? `${time}:00` : time}`;
}

function applyDateTime(value, dateId, timeId) {
  if (!value) {
    $(dateId).value = "";
    $(timeId).value = "";
    return;
  }
  const [date = "", time = ""] = value.split("T");
  $(dateId).value = date;
  $(timeId).value = time.slice(0, 8);
}

async function loadProject(pid, selectedScreenId = 0, selectedSkuId = 0) {
  $("projectInfo").textContent = "加载中…";
  try {
    const p = await api("/api/project?project_id=" + pid);
    if (p.ok === false) throw new Error(p.message || "加载演出失败");
    $("projectInfo").textContent = "";
    const b = document.createElement("b");
    b.textContent = p.name;
    $("projectInfo").appendChild(b);
    $("projectInfo").appendChild(document.createTextNode(`（project_id=${p.project_id}）`));
    const screenSel = $("screenSelect");
    screenSel.innerHTML = "";
    p.screens.forEach((s) => {
      const o = document.createElement("option");
      o.value = s.screen_id;
      o.textContent = s.name;
      o.dataset.skus = JSON.stringify(s.skus);
      screenSel.appendChild(o);
    });
    if (selectedScreenId && [...screenSel.options].some((o) => parseInt(o.value, 10) === selectedScreenId)) {
      screenSel.value = String(selectedScreenId);
    }
    renderSkus(selectedSkuId);
  } catch (e) {
    $("projectInfo").textContent = "加载失败：" + e.message;
  }
}

$("loadProjectBtn").addEventListener("click", async () => {
  const pid = parseProjectId($("projectInput").value);
  if (!pid) return alert("无法识别 project_id");
  await loadProject(pid);
});

function renderSkus(selectedSkuId = 0) {
  const opt = $("screenSelect").selectedOptions[0];
  const skuSel = $("skuSelect");
  skuSel.innerHTML = "";
  if (!opt) return;
  JSON.parse(opt.dataset.skus || "[]").forEach((k) => {
    const o = document.createElement("option");
    o.value = k.sku_id;
    o.textContent = `${k.desc} ￥${(k.price / 100).toFixed(2)} ${k.sale_flag || ""}`;
    skuSel.appendChild(o);
  });
  if (selectedSkuId && [...skuSel.options].some((o) => parseInt(o.value, 10) === selectedSkuId)) {
    skuSel.value = String(selectedSkuId);
  }
}
$("screenSelect").addEventListener("change", () => renderSkus());

function setSavedSelect(selectId, value, label) {
  const select = $(selectId);
  select.innerHTML = "";
  if (!value) return;
  const option = document.createElement("option");
  option.value = value;
  option.textContent = `已保存${label} ID ${value}`;
  select.appendChild(option);
}

function renderSavedBuyerIds(ids) {
  const box = $("buyerList");
  box.innerHTML = "";
  (ids || []).forEach((id) => {
    const label = document.createElement("label");
    label.className = "buyer-item";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = id;
    cb.checked = true;
    label.appendChild(cb);
    label.appendChild(document.createTextNode(` 已保存购票人 ID ${id}`));
    box.appendChild(label);
  });
}

async function loadBuyers(selectedBuyerIds = []) {
  const box = $("buyerList");
  box.textContent = "加载中…";
  try {
    const r = await api("/api/buyers");
    if (!r.ok) throw new Error(r.message || "加载购票人失败");
    const buyers = r.buyers || [];
    const selected = new Set((selectedBuyerIds || []).map((id) => Number(id)));
    box.innerHTML = "";
    buyers.forEach((b) => {
      const label = document.createElement("label");
      label.className = "buyer-item";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = b.buyer_id;
      cb.checked = selected.has(Number(b.buyer_id));
      label.appendChild(cb);
      label.appendChild(document.createTextNode(` ${b.name} · ${b.tel} · ${b.id_card}`));
      box.appendChild(label);
    });
    if (!buyers.length) box.textContent = "没有购票人，请先在 B 站 App 添加。";
  } catch (e) {
    box.textContent = e.message || "加载购票人失败，请确认已登录";
  }
}

$("loadBuyersBtn").addEventListener("click", async () => {
  await loadBuyers();
});

function collectConfig() {
  const buyerIds = [...document.querySelectorAll("#buyerList input:checked")].map((c) => parseInt(c.value, 10));
  const returnMonitorEnabled = $("returnMonitorEnabled").checked;
  return {
    project_id: parseProjectId($("projectInput").value),
    screen_id: parseInt($("screenSelect").value || "0", 10),
    sku_id: parseInt($("skuSelect").value || "0", 10),
    buyer_ids: buyerIds,
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

async function loadConfig() {
  const c = await api("/api/config");
  if (c.project_id) $("projectInput").value = c.project_id;
  setSavedSelect("screenSelect", c.screen_id || 0, "场次");
  setSavedSelect("skuSelect", c.sku_id || 0, "票档");
  renderSavedBuyerIds(c.buyer_ids || []);
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
    await loadProject(c.project_id, c.screen_id || 0, c.sku_id || 0);
  }
}

// ---------- 监控 ----------
function addLog(ev) {
  const log = $("log");
  const line = document.createElement("div");
  line.className = "log-line " + (ev.level || "info");
  line.textContent = `[${ev.time || ""}] ${ev.message || ""}`;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
  while (log.childElementCount > 500) log.removeChild(log.firstChild);
}

function applyStatus(s) {
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

let ws = null;
let wsReconnectTimer = null;
function connectWS() {
  if (authRedirecting || ws) return;
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
    if (authRedirecting || event.code === 1008 || !opened) {
      redirectToAuth();
      return;
    }
    if (wsReconnectTimer) clearTimeout(wsReconnectTimer);
    wsReconnectTimer = setTimeout(connectWS, 2000);
  };
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

// ---------- 验证码 ----------
async function checkCaptcha() {
  try {
    const r = await api("/api/captcha");
    if (r.pending) {
      $("capGt").value = r.gt;
      $("capChallenge").value = r.challenge;
      $("captchaModal").classList.add("show");
    }
  } catch (e) {
    addLog({ level: "warn", message: e.message || "检查验证码状态失败", time: new Date().toLocaleTimeString() });
  }
}
$("capSubmit").addEventListener("click", async () => {
  try {
    const r = await postJSON("/api/captcha", {
      validate: $("capValidate").value.trim(),
      seccode: $("capSeccode").value.trim(),
    });
    if (r.ok) {
      $("captchaModal").classList.remove("show");
      $("capValidate").value = "";
      $("capSeccode").value = "";
    } else {
      alert(r.message || "提交失败");
    }
  } catch (e) {
    alert(e.message || "提交失败");
  }
});

// ---------- 初始化 ----------
(async function init() {
  try {
    await loadConfig();
    setUser(await api("/api/login/status"));
    connectWS();
  } catch (e) {
    console.warn("初始化失败:", e);
  }
})();
