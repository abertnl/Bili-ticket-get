import { api } from "./api.js";
import { $ } from "./dom.js";

let savedReturnMonitorSkuIds = [];

export function parseProjectId(text) {
  text = (text || "").trim();
  const m = text.match(/(?:id=|project_id=|\/detail\/)(\d+)/);
  if (m) return parseInt(m[1], 10);
  if (/^\d+$/.test(text)) return parseInt(text, 10);
  return 0;
}

export async function loadProject(pid, selectedScreenId = 0, selectedSkuId = 0, selectedReturnSkuIds = []) {
  savedReturnMonitorSkuIds = (selectedReturnSkuIds || []).map((id) => Number(id)).filter(Boolean);
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
    renderSkus(selectedSkuId, savedReturnMonitorSkuIds);
  } catch (e) {
    $("projectInfo").textContent = "加载失败：" + e.message;
  }
}

$("loadProjectBtn").addEventListener("click", async () => {
  const pid = parseProjectId($("projectInput").value);
  if (!pid) return alert("无法识别 project_id");
  await loadProject(pid);
});

export function skuOptionText(k) {
  const stock = Number.isFinite(Number(k.num)) ? ` 库存提示 ${Number(k.num)}` : "";
  return `${k.desc} ￥${(k.price / 100).toFixed(2)} ${k.sale_flag || ""}${stock}`;
}

export function currentReturnSkuIds() {
  return [...document.querySelectorAll("#returnSkuList input:checked")].map((c) => parseInt(c.value, 10));
}

export function renderReturnSkuCandidates(selectedSkuIds = []) {
  const list = $("returnSkuList");
  const opt = $("screenSelect").selectedOptions[0];
  const skuSel = $("skuSelect");
  list.innerHTML = "";
  if (!opt) return;
  const skus = JSON.parse(opt.dataset.skus || "[]");
  const primarySkuId = parseInt(skuSel.value || "0", 10);
  const selected = new Set((selectedSkuIds || []).map((id) => Number(id)).filter(Boolean));
  if (primarySkuId) selected.add(primarySkuId);

  skus.forEach((k) => {
    const skuId = Number(k.sku_id);
    const label = document.createElement("label");
    label.className = "buyer-item";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = skuId;
    cb.checked = selected.has(skuId);
    if (skuId === primarySkuId) {
      cb.checked = true;
      cb.disabled = true;
    }
    const text = document.createElement("span");
    text.textContent = ` ${skuOptionText(k)}${skuId === primarySkuId ? " · 首发票档" : ""}`;
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `sku_id=${skuId}`;
    const wrap = document.createElement("span");
    wrap.appendChild(text);
    wrap.appendChild(meta);
    label.appendChild(cb);
    label.appendChild(wrap);
    list.appendChild(label);
  });
}

export function renderSkus(selectedSkuId = 0, selectedReturnSkuIds = currentReturnSkuIds()) {
  const opt = $("screenSelect").selectedOptions[0];
  const skuSel = $("skuSelect");
  skuSel.innerHTML = "";
  if (!opt) {
    $("returnSkuList").innerHTML = "";
    return;
  }
  JSON.parse(opt.dataset.skus || "[]").forEach((k) => {
    const o = document.createElement("option");
    o.value = k.sku_id;
    o.textContent = skuOptionText(k);
    skuSel.appendChild(o);
  });
  if (selectedSkuId && [...skuSel.options].some((o) => parseInt(o.value, 10) === selectedSkuId)) {
    skuSel.value = String(selectedSkuId);
  }
  renderReturnSkuCandidates(selectedReturnSkuIds);
}

$("screenSelect").addEventListener("change", () => renderSkus());
$("skuSelect").addEventListener("change", () => renderReturnSkuCandidates(currentReturnSkuIds()));

export function setSavedSelect(selectId, value, label) {
  const select = $(selectId);
  select.innerHTML = "";
  if (!value) return;
  const option = document.createElement("option");
  option.value = value;
  option.textContent = `已保存${label} ID ${value}`;
  select.appendChild(option);
}

export function renderSavedBuyerIds(ids) {
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

export function renderSavedReturnSkuIds(ids) {
  const box = $("returnSkuList");
  box.innerHTML = "";
  savedReturnMonitorSkuIds = (ids || []).map((id) => Number(id)).filter(Boolean);
  savedReturnMonitorSkuIds.forEach((id) => {
    const label = document.createElement("label");
    label.className = "buyer-item";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = id;
    cb.checked = true;
    label.appendChild(cb);
    label.appendChild(document.createTextNode(` 已保存回流候选票档 ID ${id}`));
    box.appendChild(label);
  });
}

export async function loadBuyers(selectedBuyerIds = []) {
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
