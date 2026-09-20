// 统一的请求封装：解析 JSON、归一化错误、遇到未授权跳回登录页。

let authRedirecting = false;

export function isAuthRedirecting() {
  return authRedirecting;
}

export function redirectToAuth() {
  if (authRedirecting) return;
  authRedirecting = true;
  location.replace("/");
}

export const api = async (url, opts) => {
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

export const postJSON = (url, body) =>
  api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
