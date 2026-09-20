import { api, postJSON } from "./api.js";
import { $ } from "./dom.js";

let pollTimer = null;

export function setUser(info) {
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
