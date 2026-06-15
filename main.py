"""入口：启动本地 Web 服务。

用法：
    uv run main.py
然后浏览器打开 http://127.0.0.1:8000
"""

import uvicorn

from app.config import ensure_admin_token, load_config, using_generated_admin_token, validate_server_security


def main() -> None:
    config = load_config()
    token = ensure_admin_token(config)
    validate_server_security(config)
    print(f"会员购抢票工具已启动：http://{config.server.host}:{config.server.port}")
    if using_generated_admin_token(config):
        print(f"本次临时管理 token：{token}")
    else:
        print("已启用管理 token。")
    print("仅供个人买票自用，请遵守 B 站用户协议。")
    uvicorn.run(
        "app.server:app",
        host=config.server.host,
        port=config.server.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
