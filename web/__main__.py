"""本机直跑入口：单进程单 worker。

运行：python -m web
  WEB_PORT  端口（默认 8123；与 README、web-ui/vite.config.js 代理一致——
            单一事实来源，三处同步改）
  WEB_HOST  监听地址（默认 127.0.0.1；登录用户即能触发真实云操作——
            放宽到 0.0.0.0 / 内网地址由运行者自担，spec 运行边界为
            「只监听 127.0.0.1 或内网」）
  WEB_STATE_PATH  簿记落盘路径（默认 ~/.auto-image-web/state.json；
            同一 HOME 下多实例并行时各自落册，避免互相覆盖）
  WEB_USERS_PATH  用户清单（默认仓库根 users.yaml，mtime 热载；
            范本见 users.yaml.example）
  WEB_AUTH_SECRET 认证签名密钥（缺省按机器稳定属性派生；换值即全体
            登录态失效，用于密钥轮换）
"""
import os

import uvicorn

from .app import create_app


def main():
    port = int(os.environ.get("WEB_PORT", "8123"))
    host = os.environ.get("WEB_HOST", "127.0.0.1")
    # 工厂形态：应用构造发生在事件循环就绪后——启动段的状态恢复（簿记 +
    # transcript 重放）在无运行循环环境下会崩；state_path=None 即默认位置
    uvicorn.run(
        lambda: create_app(
            state_path=os.environ.get("WEB_STATE_PATH"),
            users_path=os.environ.get("WEB_USERS_PATH"),
            auth_secret=os.environ.get("WEB_AUTH_SECRET"),
        ),
        factory=True, host=host, port=port,
    )


if __name__ == "__main__":
    main()
