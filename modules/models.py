"""
共用数据结构定义
所有模块通过这些标准化数据结构通信
"""
from dataclasses import dataclass, field
from typing import List, Optional, Literal
from datetime import datetime
from enum import Enum


class AppState(Enum):
    """应用运行状态"""
    IDLE = "休眠中"
    CAPTURING = "截图中"
    VL_RECOGNIZING = "VL识别中"
    WAITING_AGENT = "等待AI Agent"
    TYPING = "执行键鼠发送"
    HISTORY_COLLECTING = "历史采集中"
    PAUSED = "已暂停"
    EMERGENCY_STOPPED = "紧急停止"


@dataclass
class Rect:
    """选区矩形，全屏物理坐标"""
    left: int
    top: int
    width: int
    height: int

    def to_tuple(self) -> tuple:
        """返回 (left, top, right, bottom)"""
        return (self.left, self.top, self.left + self.width, self.top + self.height)

    def contains(self, x: int, y: int) -> bool:
        """边界校验：点是否在选区内"""
        return (self.left <= x <= self.left + self.width
                and self.top <= y <= self.top + self.height)

    def clamp_point(self, x: int, y: int) -> tuple:
        """将点钳制到选区范围内"""
        x = max(self.left, min(x, self.left + self.width))
        y = max(self.top, min(y, self.top + self.height))
        return (x, y)


@dataclass
class Message:
    """单条消息"""
    text: str
    sender: Literal["self", "other"]  # 我方 / 对方
    timestamp: datetime = field(default_factory=datetime.now)
    source: str = "vl"  # 来源标记：vl=VL识别, agent=AI生成


@dataclass
class VLResult:
    """VL视觉识别结果（结构化JSON）"""
    has_new_message: bool = False
    messages: List[Message] = field(default_factory=list)
    input_box: Optional[Rect] = None  # 聊天输入框矩形（图像坐标系），用于键鼠发送回复
    raw_response: str = ""  # 原始返回，用于调试
    is_valid: bool = True  # JSON格式校验是否通过
    error: str = ""  # 识别失败时的错误信息


@dataclass
class AgentResult:
    """AI Agent返回结果"""
    reply_text: str = ""
    success: bool = False
    raw_response: str = ""
    error: str = ""


@dataclass
class ComputerUseAction:
    """Computer Use Agent 单步动作"""
    action: str = "wait"  # click / type / scroll / wait / done
    coordinate: Optional[List[int]] = None  # [x, y] 选区内绝对坐标
    text: str = ""  # type 动作的输入文本
    scroll_count: int = 0  # scroll 动作的滚动次数（正=向上，负=向下）
    thought: str = ""  # 模型的思考过程
    done: bool = False  # 是否完成任务


@dataclass
class ComputerUseResult:
    """Computer Use Agent 一轮循环的结果"""
    success: bool = False
    actions: List[ComputerUseAction] = field(default_factory=list)
    total_steps: int = 0
    raw_response: str = ""
    error: str = ""
