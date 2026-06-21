# B 站会员购抢票工具

Bilibili 会员购（show.bilibili.com）本地抢票工具。基于 FastAPI 的 Web 应用，支持定时开抢、自动重试、风控处理、回流票监控和抢中推送提醒。

> 仅供个人买票自用，请遵守 B 站用户协议。

## 功能特性

- **扫码 / Cookie 登录** — 支持 B 站 App 扫码登录或手动粘贴 Cookie
- **定时开抢** — 精确到秒的开抢时间设定，支持提前预热凭据
- **自动重试** — 可配置请求间隔、最大尝试次数、限流退避策略
- **自动平衡策略** — 首发短冲刺、回流低频监控、连续限流冷却，减少无效请求
- **风控处理** — 自动识别 `-352` 风控，支持人工极验回填和第三方打码（rrocr）
- **回流票监控** — 售罄后低频监控票档状态，发现回流自动下单冲刺
- **本地复盘** — 每次运行生成 JSONL 事件文件，便于复盘限流、拥堵和售罄节奏
- **实时日志** — WebSocket 推送抢票过程中的每一步日志
- **抢中推送** — 支持 Bark、Server 酱、macOS iMessage 通知
- **Web 界面** — 全中文操作界面，浏览器打开即用

## 快速开始

### 环境要求

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)（Python 包管理器）

### 安装与启动

```bash
# 克隆项目
git clone https://github.com/abertnl/Bili-ticket-get.git
cd Bili-ticket-get

# 安装依赖
uv sync

# 启动服务
uv run main.py
```

启动后终端会打印本次临时管理 token。浏览器打开 `http://127.0.0.1:8000` 后输入 token 即可使用。

### 使用步骤

1. **登录** — 扫码登录或粘贴 Cookie
2. **配置** — 填入演出链接/ID，选择场次、票档、购票人，设置抢票参数
3. **开抢** — 点击「开始抢票」，观察实时日志，抢中后尽快前往 B 站支付

详细使用说明见 [docs/使用说明.md](docs/使用说明.md)。

## 配置说明

配置保存在项目根目录 `config.json`（已 gitignore），首次保存时自动生成。参考 `config.example.json`。

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `cookie` | 登录 Cookie（SESSDATA / bili_jct / DedeUserID） | — |
| `project_id` | 演出 ID | — |
| `screen_id` / `sku_id` | 场次 / 票档 ID | — |
| `return_monitor_sku_ids` | 回流票监控候选票档 ID 列表（同一场次内） | `[]` |
| `buyer_ids` | 购票人 ID 列表 | `[]` |
| `contact_name` / `contact_tel` | 联系人姓名 / 手机号（部分项目必填，留空使用首位购票人） | `""` |
| `count` | 购买张数 | `1` |
| `start_time` | 开抢时间（ISO 格式，留空立即开始） | `""` |
| `interval_ms` | 请求间隔（毫秒） | `800` |
| `max_attempts` | 最大尝试次数 | `300` |
| `prewarm_seconds` | 开抢前预热提前量（秒） | `30` |
| `rate_limit_backoff_ms` | 限流退避（毫秒） | `2000` |
| `rate_limit_cooldown_ms` | 连续限流冷却（毫秒） | `8000` |
| `captcha_mode` | 验证码方式：`manual` / `rrocr` | `manual` |
| `return_monitor_enabled` | 启用回流票监控 | `false` |
| `notify.bark_url` | Bark 推送地址 | `""` |
| `notify.serverchan_key` | Server 酱 Key | `""` |

启用回流票监控时，默认策略会在开抢点先对主票档短冲刺；遇到连续限流或库存不足会回到低频监控，
并在同一场次内同时检查回流候选票档。发现多个疑似可售票档时，优先尝试库存数更多的候选；
候选为空时保持兼容，只监控主票档。
每次运行会在 `runtime/grab-runs/` 下写入一份 JSONL 复盘文件，该目录已被 git 忽略。

## 项目结构

```
├── main.py                 # 启动入口
├── app/
│   ├── server.py           # FastAPI 应用（REST + WebSocket）
│   ├── grabber.py          # 抢票状态机
│   ├── config.py           # 配置模型与持久化
│   ├── notify.py           # 推送通知（Bark / Server酱 / iMessage）
│   └── bili/
│       ├── client.py       # Bilibili HTTP 客户端（Headers、Cookie、bili_ticket）
│       ├── ticket.py       # 会员购 API（演出信息、购票人、下单）
│       ├── risk.py         # 风控处理（gaia-vgate 流程）
│       ├── captcha.py      # 验证码接口（Manual / Rrocr）
│       └── errors.py       # 错误码分类
├── web/                    # 前端静态文件
│   ├── index.html
│   ├── app.js
│   └── style.css
├── config.example.json     # 配置示例
├── pyproject.toml          # 项目元数据与依赖
└── docs/
    └── 使用说明.md         # 详细使用文档
```

## 技术栈

- **后端** — FastAPI + Uvicorn + httpx
- **前端** — 原生 HTML/CSS/JS，WebSocket 实时通信
- **数据模型** — Pydantic v2
- **包管理** — uv

## 常见问题

| 现象 | 解决方法 |
|------|----------|
| 一直返回 `429` | 增大 `interval_ms`，并把 `rate_limit_cooldown_ms` 调到 8000ms 以上 |
| 触发 `-352` 风控 | 按页面提示完成人机验证，适当降低频率 |
| 加载购票人失败 | 重新登录，确认账号已添加实名购票人 |
| 售罄 (`100009`) | 启用回流票监控，等待退票释放库存 |

## 免责声明

本工具仅用于学习和个人合法购票。接口可能随 B 站更新而失效。使用过程中产生的账号风险、支付风险或其他损失由使用者自行承担。
