"""
配置管理模块
集中管理所有可配置参数，支持运行时修改
"""
from dataclasses import dataclass, field
from typing import Optional
import json
import os

@dataclass
class Config:
    """全局配置"""

    # ===== 轮询与风控参数 =====
    # 定时快照随机间隔范围（秒）
    poll_interval_min: int = 30
    poll_interval_max: int = 90

    # 单位时间（1小时）内自动回复硬上限
    reply_limit_per_hour: int = 20

    # AI回复后思考等待范围（秒），在此范围内随机
    think_wait_min: float = 5.0
    think_wait_max: float = 15.0

    # ===== VL 模型配置（云端API） =====
    # provider: "dashscope"(阿里百炼) | "openrouter" | "custom"(自部署)
    vl_provider: str = "dashscope"
    vl_api_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    vl_api_key: str = ""
    vl_model_name: str = "qwen-vl-max"
    vl_timeout: int = 30  # 超时秒数

    # ===== AI Agent 配置（云端API） =====
    agent_provider: str = "dashscope"
    agent_api_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    agent_api_key: str = ""
    agent_model_name: str = "qwen-plus"
    agent_timeout: int = 30

    # ===== 上下文管理 =====
    max_context_messages: int = 30  # 上下文窗口最大消息数
    storage_path: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    screenshot_path: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")

    # ===== 历史采集参数 =====
    history_scroll_steps: int = 5  # 每次翻页滚轮步数
    history_scroll_wait: float = 0.8  # 滚动后渲染等待时间
    history_similarity_threshold: float = 0.92  # 图像相似度终止阈值
    history_max_pages: int = 100  # 最大翻页数保护

    # ===== 键鼠执行参数 =====
    mouse_speed_min: float = 200.0  # 像素/秒
    mouse_speed_max: float = 500.0
    mouse_jitter: int = 3  # 坐标噪声幅度（像素）
    typing_delay_min: float = 0.05  # 打字间隔最小（秒）
    typing_delay_max: float = 0.15  # 打字间隔最大
    # 鼠标移动模式: "human"=贝塞尔拟人移动, "fast"=瞬时快速移动
    mouse_move_mode: str = "human"

    # 选区持久化 [left, top, width, height]，None=用默认位置
    selection_rect: Optional[list] = None

    # ===== Computer Use Agent 参数 =====
    # 运行模式: "vl_agent"=VL+Agent两段式, "computer_use"=Computer Use循环
    run_mode: str = "vl_agent"
    cu_max_steps: int = 10  # 单轮 Computer Use 最大循环步数
    cu_step_delay_min: float = 0.5  # 每步动作前随机停顿最小（秒）
    cu_step_delay_max: float = 2.0  # 每步动作前随机停顿最大（秒）
    cu_screenshot_interval: float = 0.8  # 每步执行后等待画面刷新时间
    cu_goal: str = ""  # Computer Use 操作目标提示词（空则用默认）

    # ===== 全局快捷键 =====
    hotkey_start_pause: str = "ctrl+alt+s"
    hotkey_history_collect: str = "ctrl+alt+h"
    hotkey_emergency_stop: str = "ctrl+alt+x"

    # ===== 日志 =====
    log_path: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    log_level: str = "INFO"  # DEBUG / INFO / WARNING / ERROR

    def save(self, path: str = None):
        """保存配置到JSON文件"""
        path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        data = self.__dict__.copy()
        # 路径转为相对路径保存
        base = os.path.dirname(os.path.abspath(__file__))
        for key in ("storage_path", "screenshot_path", "log_path"):
            if data.get(key):
                data[key] = os.path.relpath(data[key], base)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str = None) -> "Config":
        """从JSON文件加载配置"""
        path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        if not os.path.exists(path):
            return cls()
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        base = os.path.dirname(os.path.abspath(__file__))
        # 相对路径转绝对路径
        for key in ("storage_path", "screenshot_path", "log_path"):
            if data.get(key):
                data[key] = os.path.join(base, data[key])
        # 只加载类中定义的字段
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)
