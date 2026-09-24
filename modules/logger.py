"""
日志模块
记录截图时间、VL识别结果、Agent返回内容、键鼠动作、异常信息
支持控制台输出 + 文件持久化，支持从控制面板实时查看
"""
import logging
import os
from datetime import datetime
from typing import Callable, List
from PyQt5.QtCore import QObject, pyqtSignal


class LogSignal(QObject):
    """用于向GUI推送日志的信号"""
    log_emitted = pyqtSignal(str, str)  # (level, message)


class Logger:
    """单例日志器"""

    _instance = None
    _signal = LogSignal()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, log_path: str = None, level: str = "INFO"):
        if getattr(self, "_initialized", False):
            self.update_config(log_path, level)
            return
        self._initialized = True
        self._setup(log_path, level)

    def _setup(self, log_path: str, level: str):
        os.makedirs(log_path, exist_ok=True)
        log_file = os.path.join(log_path, f"app_{datetime.now().strftime('%Y%m%d')}.log")

        self.logger = logging.getLogger("WeChatAssistant")
        self.logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        self.logger.handlers.clear()

        fmt = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # 文件handler
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        self.logger.addHandler(fh)

        # 控制台handler
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        self.logger.addHandler(ch)

    def update_config(self, log_path: str = None, level: str = None):
        if log_path or level:
            self._setup(log_path or self.logger.handlers[0].baseFilename.rsplit("\\", 1)[0],
                        level or logging.getLevelName(self.logger.level))

    @classmethod
    def get_signal(cls) -> LogSignal:
        return cls._signal

    def _emit(self, level: str, msg: str):
        self._signal.log_emitted.emit(level, msg)

    def debug(self, msg: str):
        self.logger.debug(msg)
        self._emit("DEBUG", msg)

    def info(self, msg: str):
        self.logger.info(msg)
        self._emit("INFO", msg)

    def warning(self, msg: str):
        self.logger.warning(msg)
        self._emit("WARNING", msg)

    def error(self, msg: str):
        self.logger.error(msg)
        self._emit("ERROR", msg)


# 全局单例
log = Logger(
    log_path=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"),
    level="INFO",
)
