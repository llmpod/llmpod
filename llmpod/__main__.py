"""统一 CLI 入口: python -m llmpod {webserver|proxyserver|node}"""

import asyncio
import argparse


def main():
    parser = argparse.ArgumentParser(description="LLMPod: AI 推理网关平台")
    parser.add_argument(
        "mode",
        choices=["webserver", "proxyserver", "node"],
        help="启动模式: 管理端（manager+webserver）、API 网关（proxyserver）、节点代理",
    )
    args = parser.parse_args()

    if args.mode == "webserver":
        from llmpod.app import main as control_plane_main

        asyncio.run(control_plane_main())
    elif args.mode == "proxyserver":
        import uvicorn

        from llmpod.config.settings import settings
        from llmpod.core.logging import setup_logging

        # uvicorn 只配 uvicorn.* logger；app 侧 stdlib logger（llmpod.proxyserver*）
        # 不接线会落到 lastResort（≥WARNING 裸消息），INFO 应用日志整体隐形
        setup_logging(settings.log_level)
        uvicorn.run(
            "llmpod.apps.proxyserver.app:app",
            host=settings.proxy_host,
            port=settings.proxy_port,
        )
    elif args.mode == "node":
        from llmpod.apps.node.app import main as node_main

        asyncio.run(node_main())


if __name__ == "__main__":
    main()
