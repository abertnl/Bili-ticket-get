"""入口：启动本地 Web 服务。

用法：
    uv run main.py
然后浏览器打开 http://127.0.0.1:8000
"""

import uvicorn

from app.config import load_config


def main() -> None:
    config = load_config()
    print(f"会员购抢票工具已启动：http://{config.server.host}:{config.server.port}")
    print("仅供个人买票自用，请遵守 B 站用户协议。")
    uvicorn.run(
        "app.server:app",
        host=config.server.host,
        port=config.server.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
