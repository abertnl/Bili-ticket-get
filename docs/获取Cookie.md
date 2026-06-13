# 如何提取 B 站 Cookie

本工具优先推荐**扫码登录**（登录页直接扫码即可，无需手动提取 Cookie）。
如果你想用 Cookie 登录，或扫码不便，可按下面方法手动获取。

> 安全提示：Cookie 等同于你的账号登录凭证，**切勿分享给任何人**、不要截图发群、不要提交到 Git。
> 本工具会把它保存在本地 `config.json`，该文件已被 `.gitignore` 忽略。

需要的关键字段：`SESSDATA`、`bili_jct`、`DedeUserID`（三者缺一不可）。

---

## 准备工作

1. 用电脑浏览器（Chrome / Edge / Firefox 均可）登录 <https://show.bilibili.com>（会员购）。
2. 确认右上角已显示你的头像 / 昵称（即已登录状态）。

---

## 方法一：从开发者工具的 Network 复制整段 Cookie（推荐）

这种方式能一次性拿到完整 Cookie 字符串，直接粘贴到工具里即可。

1. 在 `show.bilibili.com` 页面按 `F12`（或右键 → 检查）打开开发者工具。
2. 切到 **Network（网络）** 标签。
3. 按 `F5` 刷新页面，让请求列表出现内容。
4. 在请求列表里点任意一个发往 `show.bilibili.com` 的请求（比如 `nav`、`index` 之类）。
5. 右侧找到 **Headers（标头）** → **Request Headers（请求标头）** → 找到 `Cookie:` 这一行。
6. 复制 `Cookie:` 后面那一长串内容（从 `buvid3=...` 一直到末尾）。
7. 粘贴到本工具「登录」页的 **Cookie 登录** 输入框，点「使用 Cookie 登录」。

复制到的内容大致形如（这里是占位示例）：

```
buvid3=XXXX; SESSDATA=xxxxxxxx; bili_jct=xxxxxxxx; DedeUserID=12345678; DedeUserID__ckMd5=xxxx; ...
```

只要其中包含 `SESSDATA`、`bili_jct`、`DedeUserID` 即可，多余字段不影响。

---

## 方法二：从 Application / 存储里逐个复制

适合只想取关键三项的人。

### Chrome / Edge

1. `F12` 打开开发者工具，切到 **Application（应用）** 标签。
2. 左侧 **Storage（存储）** → **Cookies** → 选择 `https://show.bilibili.com`（也可在 `https://www.bilibili.com` 找到）。
3. 在右侧列表里依次找到并复制这几项的 **Value**：
   - `SESSDATA`
   - `bili_jct`
   - `DedeUserID`
4. 按下面格式自己拼成一行（用 `; ` 分隔），粘贴到工具里：

```
SESSDATA=刚复制的值; bili_jct=刚复制的值; DedeUserID=刚复制的值
```

### Firefox

1. `F12` → **存储（Storage）** 标签 → **Cookie** → 选 `https://show.bilibili.com`。
2. 同样复制 `SESSDATA`、`bili_jct`、`DedeUserID` 的值，按上面格式拼接。

---

## 字段说明

| 字段 | 作用 | 来源 |
| --- | --- | --- |
| `SESSDATA` | 登录会话凭证（最关键） | Cookie |
| `bili_jct` | CSRF Token，下单接口必需 | Cookie |
| `DedeUserID` | 你的用户 UID | Cookie |

---

## 常见问题

- **粘贴后提示「Cookie 无效或已过期」**
  - 检查是否漏了 `SESSDATA` / `bili_jct` / `DedeUserID` 任意一项。
  - Cookie 有有效期，过期后需重新登录浏览器再复制；或直接改用扫码登录。
  - 确认是从**已登录**的浏览器复制的。

- **多久会失效？**
  - `SESSDATA` 通常有效期较长（约一个月），但异地登录、修改密码、主动退出都会让它失效。

- **可以用手机抓包得到的 Cookie 吗？**
  - 可以，只要包含上述三项即可；但更推荐直接用本工具的扫码登录，省事且不易出错。

- **换了网络 / 频繁请求被风控（-352 / 412）怎么办？**
  - 这与 Cookie 是否正确无关，属于风控。适当增大「请求间隔」，必要时更换网络环境，详见 `README.md`。
