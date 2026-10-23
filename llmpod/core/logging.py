"""使用 loguru 的结构化日志配置。"""

from __future__ import annotations

import logging
import sys

from loguru import logger


def setup_logging(level: str = "info") -> None:
    """配置 loguru 以实现结构化日志。

    用 loguru sink 替换标准库日志处理器。
    """
    # 移除默认处理器
    logger.remove()

    # 添加带彩色输出的 stderr sink
    logger.add(
        sys.stderr,
        level=level.upper(),
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>",
        colorize=True,
    )

    # 拦截标准库日志
    class InterceptHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno
            # 从当前帧起向上跳过 logging 模块和本文件的所有内部帧，
            # 直到定位到真正的调用方。
            frame, depth = logging.currentframe(), 1
            while frame and frame.f_code.co_filename in (logging.__file__, __file__):
                frame = frame.f_back
                depth += 1
            logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

    # 将第三方日志记录器设置为 WARNING 级别
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy", "websockets"):
        logging.getLogger(name).setLevel(logging.WARNING)
