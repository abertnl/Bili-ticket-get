import { api, postJSON } from "./api.js";
import { $ } from "./dom.js";
import { addLog } from "./log.js";

export async function checkCaptcha() {
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
