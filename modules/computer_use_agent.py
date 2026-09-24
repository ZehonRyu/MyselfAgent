"""
Computer Use Agent 模块（轻量自实现版）
========================================
不依赖 gui-agents/anthropic computer-use，自己用 OpenAI 兼容 API 实现
"截图 → 多模态LLM → 动作JSON → SendInput执行 → 循环" 的通用 GUI Agent。

核心循环：
1. 截图选区
2. 发送给 VL 多模态模型，附带通用聊天界面分析提示词
3. 模型返回动作 JSON
4. 通过 SendInput 执行动作（真人化鼠标移动 + 打字）
5. 循环直到模型返回 done=true 或达到最大步数

动作格式：
  {
    "thought": "分析当前界面...",
    "action": "click" | "type" | "scroll" | "wait" | "done",
    "coordinate": [x, y],          // 选区内绝对屏幕坐标
    "text": "...",                 // type 动作要输入的文本
    "scroll_count": 3,             // scroll 动作滚动次数（正=向上, 负=向下）
    "done": false                  // 是否完成任务
  }

通用聊天软件提示词：
  不限定微信，覆盖 QQ/钉钉/Telegram/飞书 等常见 PC 聊天界面。
  让模型自行识别界面布局（输入框、发送按钮、消息气泡）。

真人化键鼠：
  - 鼠标移动：贝塞尔曲线轨迹 + 加速度 + 像素级随机抖动
  - 打字：随机间隔 + 偶发长停顿 + 轻微错误修正模拟
  - 所有动作前随机短停顿

安全约束：
  - 所有坐标用 rect.clamp_point 钳制到选区内，绝不越界
  - 单轮最大步数保护
  - 每步检查紧急停止标志
  - 所有外部 API 调用超时捕获、JSON 格式校验
"""
import base64
import json
import math
import random
import re
import time
import ctypes
import ctypes.wintypes
from io import BytesIO
from typing import Callable, List, Optional

import requests
from PIL import Image

from .models import Rect, ComputerUseAction, ComputerUseResult
from .logger import log


# ================================================================
#  SendInput 常量与结构（Windows 键鼠模拟）
# ================================================================
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_KEYDOWN = 0x0000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

WHEEL_DELTA = 120
VK_RETURN = 0x0D  # 回车键虚拟键码


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
    ]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("_input",)
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("_input", _INPUT_UNION),
    ]


def _send_mouse_input(flags: int, dx: int = 0, dy: int = 0,
                      mouse_data: int = 0):
    """发送一次鼠标事件"""
    extra = ctypes.c_ulong(0)
    mi = _MOUSEINPUT(
        dx=dx, dy=dy, mouseData=mouse_data,
        dwFlags=flags, time=0,
        dwExtraInfo=ctypes.addressof(extra),
    )
    inp = _INPUT(type=INPUT_MOUSE)
    inp._input.mi = mi  # type: ignore
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def _send_unicode_char(ch: str):
    """通过 SendInput 发送一个 Unicode 字符（支持中文）"""
    extra = ctypes.c_ulong(0)
    ki_down = _KEYBDINPUT(
        wVk=0, wScan=ord(ch),
        dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYDOWN,
        time=0, dwExtraInfo=ctypes.addressof(extra),
    )
    ki_up = _KEYBDINPUT(
        wVk=0, wScan=ord(ch),
        dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
        time=0, dwExtraInfo=ctypes.addressof(extra),
    )
    inp_down = _INPUT(type=INPUT_KEYBOARD)
    inp_down._input.ki = ki_down  # type: ignore
    inp_up = _INPUT(type=INPUT_KEYBOARD)
    inp_up._input.ki = ki_up  # type: ignore
    arr = (_INPUT * 2)(inp_down, inp_up)
    ctypes.windll.user32.SendInput(2, ctypes.byref(arr), ctypes.sizeof(inp_down))


def _press_enter():
    """发送一次回车键按下+抬起（用于发送聊天消息）"""
    extra = ctypes.c_ulong(0)
    ki_down = _KEYBDINPUT(
        wVk=VK_RETURN, wScan=0,
        dwFlags=KEYEVENTF_KEYDOWN, time=0,
        dwExtraInfo=ctypes.addressof(extra),
    )
    ki_up = _KEYBDINPUT(
        wVk=VK_RETURN, wScan=0,
        dwFlags=KEYEVENTF_KEYUP, time=0,
        dwExtraInfo=ctypes.addressof(extra),
    )
    inp_down = _INPUT(type=INPUT_KEYBOARD)
    inp_down._input.ki = ki_down  # type: ignore
    inp_up = _INPUT(type=INPUT_KEYBOARD)
    inp_up._input.ki = ki_up  # type: ignore
    arr = (_INPUT * 2)(inp_down, inp_up)
    ctypes.windll.user32.SendInput(2, ctypes.byref(arr), ctypes.sizeof(inp_down))


def _get_cursor_pos() -> tuple:
    """获取当前鼠标物理坐标"""
    point = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return (point.x, point.y)


def _set_cursor_pos(x: int, y: int):
    """直接设置鼠标位置（绝对坐标）"""
    ctypes.windll.user32.SetCursorPos(x, y)


# ================================================================
#  真人化鼠标轨迹生成
# ================================================================
def _bezier_curve(p0, p1, p2, p3, steps: int) -> List[tuple]:
    """三阶贝塞尔曲线，返回路径点列表"""
    points = []
    for i in range(steps + 1):
        t = i / steps
        mt = 1 - t
        x = (mt ** 3) * p0[0] + 3 * (mt ** 2) * t * p1[0] \
            + 3 * mt * (t ** 2) * p2[0] + (t ** 3) * p3[0]
        y = (mt ** 3) * p0[1] + 3 * (mt ** 2) * t * p1[1] \
            + 3 * mt * (t ** 2) * p2[1] + (t ** 3) * p3[1]
        points.append((int(x), int(y)))
    return points


def _human_move_to(target_x: int, target_y: int,
                   speed_min: float = 200.0,
                   speed_max: float = 500.0,
                   jitter: int = 3,
                   mode: str = "human"):
    """
    鼠标移动：mode="human" 贝塞尔拟人轨迹；mode="fast" 瞬时直达

    human 模式：
    1. 起点 = 当前鼠标位置
    2. 生成两个随机控制点（让轨迹弯曲）
    3. 距离越长步数越多，速度随机
    4. 每步加入 0~jitter 像素的随机抖动
    5. 最后精确落到目标点
    """
    if mode == "fast":
        _set_cursor_pos(target_x, target_y)
        time.sleep(0.03)
        return

    start_x, start_y = _get_cursor_pos()
    dist = math.hypot(target_x - start_x, target_y - start_y)
    if dist < 2:
        _set_cursor_pos(target_x, target_y)
        return

    # 控制点：在起点和终点连线两侧随机偏移
    mid_x = (start_x + target_x) / 2
    mid_y = (start_y + target_y) / 2
    offset = min(dist * 0.3, 200)
    cp1 = (mid_x + random.uniform(-offset, offset),
           mid_y + random.uniform(-offset, offset))
    cp2 = (mid_x + random.uniform(-offset, offset),
           mid_y + random.uniform(-offset, offset))

    # 步数：距离 / 速度，再随机化
    speed = random.uniform(speed_min, speed_max)
    steps = max(8, int(dist / speed * 60))  # 60fps 基准

    path = _bezier_curve(
        (start_x, start_y), cp1, cp2, (target_x, target_y), steps
    )

    step_delay = 1.0 / 60
    for i, (px, py) in enumerate(path):
        if i == len(path) - 1:
            # 最后一步精确落点
            _set_cursor_pos(target_x, target_y)
        else:
            jx = px + random.randint(-jitter, jitter)
            jy = py + random.randint(-jitter, jitter)
            _set_cursor_pos(jx, jy)
        time.sleep(step_delay + random.uniform(0, 0.005))


def _human_click(x: int, y: int,
                 speed_min: float = 200.0,
                 speed_max: float = 500.0,
                 jitter: int = 3,
                 move_mode: str = "human"):
    """真人化点击：移动 → 按下 → 随机停顿 → 抬起"""
    _human_move_to(x, y, speed_min, speed_max, jitter, mode=move_mode)
    time.sleep(random.uniform(0.02, 0.08))  # 到达后微停顿
    _send_mouse_input(MOUSEEVENTF_LEFTDOWN)
    time.sleep(random.uniform(0.04, 0.12))  # 按住时长
    _send_mouse_input(MOUSEEVENTF_LEFTUP)
    log.debug(f"点击完成: ({x},{y})")


def _human_type(text: str,
                delay_min: float = 0.05,
                delay_max: float = 0.15):
    """
    真人化打字：逐字符发送，随机间隔，偶发长停顿

    中文等非 ASCII 字符走 Unicode SendInput。
    """
    for i, ch in enumerate(text):
        _send_unicode_char(ch)
        # 随机打字间隔
        delay = random.uniform(delay_min, delay_max)
        # 5% 概率长停顿（模拟思考）
        if random.random() < 0.05:
            delay += random.uniform(0.3, 1.0)
        time.sleep(delay)
    log.debug(f"输入完成: {text[:30]}...")


def _human_scroll(count: int,
                  delay_min: float = 0.05,
                  delay_max: float = 0.12):
    """
    真人化滚轮：逐次发送，随机间隔
    count > 0 向上滚, count < 0 向下滚
    """
    direction = 1 if count > 0 else -1
    n = abs(count)
    for _ in range(n):
        _send_mouse_input(MOUSEEVENTF_WHEEL, mouse_data=WHEEL_DELTA * direction)
        time.sleep(random.uniform(delay_min, delay_max))
    log.debug(f"滚动完成: {count} 次 ({'上' if direction > 0 else '下'})")


# ================================================================
#  通用聊天界面分析提示词
# ================================================================
CU_SYSTEM_PROMPT = """你是一个通用 PC 聊天软件界面操作 Agent。你将收到聊天窗口的截图，需要分析界面并输出下一步操作动作。

## 支持的聊天软件
本工具不限定特定软件，支持但不限于：
- 微信（绿色气泡=自己，白色气泡=对方，左下输入框，右下发送按钮）
- QQ（类似布局，气泡颜色可能不同）
- 钉钉（DingTalk，输入框在底部，发送按钮在右下）
- 飞书（Lark，输入框在底部）
- Telegram Desktop
- 其他常见 PC 聊天软件

## 你的任务
根据用户给出的"回复目标"（在 user 消息中），完成一次完整的"查看新消息 → 输入回复 → 点击发送"流程。
每一步你只需输出一个动作，系统会执行后再次截图让你判断下一步。

## 动作格式（严格 JSON，不要输出任何其他文字）
{
  "thought": "简短描述你对当前界面的分析和这一步要做什么",
  "action": "动作类型",
  "coordinate": [x, y],
  "text": "要输入的文本",
  "scroll_count": 滚动次数,
  "sent_to": "刚发送消息给谁（仅发送成功的那个 click 动作才填，其他动作省略此字段）",
  "done": false
}

## 动作类型
- "click": 点击 coordinate 指定的坐标。coordinate 必填。
- "type": 先点击 coordinate 定位输入框，然后输入 text 文本。coordinate 和 text 必填。
  可选字段 "send_with_enter": true —— 输入完成后自动按回车键发送消息（推荐，比点击发送按钮更可靠）。
- "send": 按回车键发送当前输入框中的消息。无需 coordinate。用于消息已输入但未发出的情况。
- "scroll": 在 coordinate 指定的区域滚动鼠标滚轮。coordinate 必填（指向要滚动的列表/区域，如联系人列表）。scroll_count > 0 向上滚，< 0 向下滚。
- "wait": 不做任何操作，等待界面变化。通常用于发送后等待消息渲染。
- "done": 任务完成，停止循环。done 字段设为 true。

## 重要规则
1. coordinate 是截图中的像素坐标，左上角为 [0,0]，右下角为 [图片宽度, 图片高度]。
   你给出的坐标会被映射回实际屏幕坐标执行，必须在截图范围内。
2. 每次只输出一个动作。不要试图在一次回复里完成多步。
3. 发送消息**优先用 send 动作（按回车）或 type 的 send_with_enter:true**，不要反复点击发送按钮（按钮面积小容易点偏）。
   典型流程：click 输入框 → type 回复内容（带 send_with_enter:true）→ wait 等待 → done
4. 发送成功（消息出现在聊天区域）的那个动作（type 带 send_with_enter 或 send），必须带 "sent_to": "当前聊天对象的名字"。
5. 系统会在 user 消息中给出"已发送名单"（任务记忆）：名单里的对象严禁重复发送；
   逐个对照名单检查还剩哪些目标未处理，直到全部完成才输出 done。
   名单里的对象不需要再点击验证，直接忽略。
6. 回复语气要自然、口语化、简短（1-2句话），像真人打字，不要用 markdown。
7. 如果界面没有新消息或不需要回复，直接输出 done。
8. 如果连续两次点击同一坐标后界面无变化，说明没命中目标，应换一个坐标或改用其他动作。
9. 只输出 JSON，不要有 markdown 标记、不要有解释文字。"""


# ================================================================
#  Computer Use Agent 主类
# ================================================================
class ComputerUseAgent:
    """
    轻量版 Computer Use Agent

    调用 OpenAI 兼容的多模态 API，循环：截图 → LLM → 动作 → 执行。
    """

    def __init__(
        self,
        api_url: str,
        api_key: str,
        model_name: str,
        timeout: int = 60,
        provider: str = "openrouter",
        rect: Rect = None,
    ):
        self._api_url = api_url
        self._api_key = api_key
        self._model = model_name
        self._timeout = timeout
        self._provider = provider
        self._rect = rect or Rect(0, 0, 100, 100)

        # 真人化参数
        self._speed_min = 200.0
        self._speed_max = 500.0
        self._jitter = 3
        self._type_delay_min = 0.05
        self._type_delay_max = 0.15
        self._move_mode = "human"  # "human"=拟人轨迹 / "fast"=快速直达

        # 点击穿透回调（由外部注入：执行动作前让 Overlay 蒙版穿透，避免拦截点击）
        self._enter_click_through = None
        self._exit_click_through = None
        self._overlay_hwnd = None

        # 任务记忆：已发送过消息的目标（跨步骤/跨轮次，防止重复发送与来回找不到）
        self._sent_targets: List[str] = []

        # 步骤间停顿
        self._step_delay_min = 0.5
        self._step_delay_max = 2.0
        self._screenshot_wait = 0.8
        self._max_steps = 10

        # 紧急停止
        self._stop_flag = False

    def update_config(self, api_url: str = None, api_key: str = None,
                      model_name: str = None, timeout: int = None,
                      provider: str = None):
        if api_url is not None:
            self._api_url = api_url
        if api_key is not None:
            self._api_key = api_key
        if model_name is not None:
            self._model = model_name
        if timeout is not None:
            self._timeout = timeout
        if provider is not None:
            self._provider = provider

    def set_rect(self, rect: Rect):
        """更新操作选区"""
        self._rect = rect

    def set_move_mode(self, mode: str):
        """设置鼠标移动模式: "human"=拟人轨迹 / "fast"=快速直达"""
        self._move_mode = mode if mode in ("human", "fast") else "human"

    def set_click_through(self, enter_cb, exit_cb):
        """注入点击穿透回调（Overlay.enter/exit_capture_mode）

        动作执行期间清除 Overlay 蒙版，让 SendInput 点击穿透到底层聊天窗口。
        """
        self._enter_click_through = enter_cb
        self._exit_click_through = exit_cb

    def set_overlay_hwnd(self, hwnd: int):
        """记录 Overlay 窗口句柄，用于动作执行期间直接切换 OS 级点击穿透"""
        self._overlay_hwnd = hwnd

    @staticmethod
    def _set_window_click_through(hwnd: int, enable: bool):
        """直接通过 Win32 API 设置/取消窗口的 WS_EX_TRANSPARENT 扩展样式

        线程安全（user32 调用可跨线程），立即生效，不依赖 Qt 事件循环。
        """
        if not hwnd:
            return
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x20
        WS_EX_LAYERED = 0x80000
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        if enable:
            new_style = style | WS_EX_TRANSPARENT | WS_EX_LAYERED
        else:
            new_style = style & ~WS_EX_TRANSPARENT
        if new_style != style:
            user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, new_style)

    def _begin_click_through(self):
        """进入点击穿透：先 OS 级立即穿透，再通知 Overlay 清除蒙版"""
        try:
            self._set_window_click_through(self._overlay_hwnd, True)
        except Exception as e:
            log.warning(f"设置窗口穿透样式失败: {e}")
        if self._enter_click_through:
            try:
                self._enter_click_through()
            except Exception as e:
                log.warning(f"进入点击穿透失败: {e}")
        time.sleep(0.2)  # 等待穿透生效/蒙版清除

    def _end_click_through(self):
        """退出点击穿透：先恢复 OS 样式（立即），再通知 Overlay 恢复蒙版"""
        try:
            self._set_window_click_through(self._overlay_hwnd, False)
        except Exception as e:
            log.warning(f"恢复窗口样式失败: {e}")
        if self._exit_click_through:
            try:
                self._exit_click_through()
            except Exception as e:
                log.warning(f"退出点击穿透失败: {e}")

    def set_human_params(self, speed_min: float, speed_max: float,
                         jitter: int, type_delay_min: float,
                         type_delay_max: float, step_delay_min: float,
                         step_delay_max: float, screenshot_wait: float,
                         max_steps: int):
        """更新真人化与循环参数"""
        self._speed_min = speed_min
        self._speed_max = speed_max
        self._jitter = jitter
        self._type_delay_min = type_delay_min
        self._type_delay_max = type_delay_max
        self._step_delay_min = step_delay_min
        self._step_delay_max = step_delay_max
        self._screenshot_wait = screenshot_wait
        self._max_steps = max_steps

    def stop(self):
        """请求紧急停止"""
        self._stop_flag = True
        log.warning("Computer Use Agent 收到停止信号")

    def reset_stop(self):
        self._stop_flag = False

    def reset_task_memory(self):
        """清空任务记忆（已发送名单），启动新一轮自动化时调用"""
        self._sent_targets = []

    @staticmethod
    def _images_similar(img1: Image.Image, img2: Image.Image,
                        threshold: float = 2.0) -> bool:
        """判断两张截图是否几乎相同（动作无效果）。
        缩小到 64x64 后计算平均像素差，低于 threshold 视为无变化。"""
        if img1 is None or img2 is None:
            return False
        try:
            s1 = img1.resize((64, 64), Image.BILINEAR).convert("L")
            s2 = img2.resize((64, 64), Image.BILINEAR).convert("L")
            import numpy as np
            diff = float(np.mean(np.abs(np.asarray(s1, dtype=float)
                                        - np.asarray(s2, dtype=float))))
            return diff < threshold
        except Exception:
            return False

    # ================================================================
    #  图片转 base64
    # ================================================================
    @staticmethod
    def _image_to_base64(img: Image.Image, max_size: int = 1280) -> str:
        """PIL.Image 转 base64 data URL"""
        if max(img.size) > max_size:
            ratio = max_size / max(img.size)
            new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
            img = img.resize(new_size, Image.LANCZOS)
        buffered = BytesIO()
        img.save(buffered, format="JPEG", quality=85)
        b64 = base64.b64encode(buffered.getvalue()).decode()
        return f"data:image/jpeg;base64,{b64}"

    # ================================================================
    #  调用 VL 模型获取下一步动作
    # ================================================================
    def _call_llm(self, img_b64: str, user_goal: str,
                 history_actions: List[dict],
                 no_effect: bool = False) -> Optional[dict]:
        """
        调用多模态 LLM，返回解析后的动作 dict，失败返回 None

        history_actions: 之前执行过的动作摘要，供模型理解上下文
        no_effect: 上一步动作后画面无变化，提醒模型换策略
        """
        if not self._api_url or not self._api_key:
            log.error("Computer Use API URL 或 Key 未配置")
            return None

        try:
            # 构造历史动作描述
            history_text = self._format_history(history_actions)

            # 任务记忆：已发送名单（防止重复发送与来回找目标）
            if self._sent_targets:
                sent_text = "、".join(self._sent_targets)
                sent_note = (f"\n\n## 已发送名单（任务记忆）\n"
                             f"{sent_text}\n"
                             f"以上对象已成功发送过消息，**严禁重复发送**；"
                             f"也不必再点击它们确认。若所有目标均已发送完毕，直接返回 done。")
            else:
                sent_note = ""

            # 动作无效果提醒
            effect_note = (
                "\n\n## ⚠ 上一步动作后画面无变化\n"
                "说明上一步点击/滚动可能未命中目标。请：\n"
                "- 换一个不同的坐标再试（不要重复点击同一位置）；\n"
                "- 或改用其他动作（如发送消息用 send 回车而非点击发送按钮）。"
                if no_effect else ""
            )

            user_content = [
                {
                    "type": "text",
                    "text": (f"## 回复目标\n{user_goal}\n\n"
                             f"## 已执行的动作\n{history_text}\n"
                             f"{sent_note}"
                             f"{effect_note}\n"
                             f"## 当前界面截图\n"
                             f"截图尺寸即为可选操作范围，coordinate 坐标请基于此截图。"
                             f"请分析界面并输出下一步动作 JSON。"),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": img_b64},
                },
            ]

            payload = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": CU_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "max_tokens": 500,
                "temperature": 0.4,
            }

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            }
            if self._provider == "openrouter":
                headers["HTTP-Referer"] = "http://localhost"
                headers["X-Title"] = "ChatAssistantCU"

            log.info(f"调用 Computer Use LLM: {self._model}")
            resp = requests.post(
                self._api_url,
                headers=headers,
                json=payload,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            raw_text = data["choices"][0]["message"]["content"]
            log.debug(f"CU LLM原始返回: {raw_text[:300]}")

            return self._parse_action(raw_text)

        except requests.exceptions.Timeout:
            log.error(f"CU LLM请求超时({self._timeout}s)")
            return None
        except requests.exceptions.RequestException as e:
            log.error(f"CU LLM请求失败: {e}")
            return None
        except (KeyError, IndexError) as e:
            log.error(f"CU LLM返回格式异常: {e}")
            return None
        except Exception as e:
            log.error(f"CU LLM未知异常: {e}")
            return None

    @staticmethod
    def _format_history(history_actions: List[dict]) -> str:
        """把已执行动作格式化为文本摘要"""
        if not history_actions:
            return "（无，这是第一步）"
        lines = []
        for i, a in enumerate(history_actions, 1):
            action = a.get("action", "?")
            thought = a.get("thought", "")
            if action == "click":
                coord = a.get("coordinate", [0, 0])
                lines.append(f"{i}. click({coord[0]},{coord[1]}) - {thought}")
            elif action == "type":
                text = a.get("text", "")[:30]
                lines.append(f"{i}. type(\"{text}\") - {thought}")
            elif action == "scroll":
                sc = a.get("scroll_count", 0)
                lines.append(f"{i}. scroll({sc}) - {thought}")
            elif action == "wait":
                lines.append(f"{i}. wait - {thought}")
            elif action == "done":
                lines.append(f"{i}. done - {thought}")
            else:
                lines.append(f"{i}. {action} - {thought}")
        return "\n".join(lines)

    @staticmethod
    def _parse_action(raw_text: str) -> Optional[dict]:
        """从 LLM 返回文本中提取并校验动作 JSON"""
        json_str = ComputerUseAgent._extract_json(raw_text)
        if json_str is None:
            log.error(f"CU动作无法提取JSON: {raw_text[:200]}")
            return None
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            log.error(f"CU动作JSON解析失败: {e}")
            return None

        # 字段校验与归一化
        action = data.get("action", "wait")
        if action not in ("click", "type", "scroll", "wait", "done"):
            log.warning(f"CU动作类型未知: {action}，降级为 wait")
            action = "wait"
            data["action"] = action

        coord = data.get("coordinate")
        if coord is not None:
            if not (isinstance(coord, list) and len(coord) == 2):
                log.warning(f"CU coordinate格式异常: {coord}，置空")
                coord = None
            else:
                coord = [int(coord[0]), int(coord[1])]
        data["coordinate"] = coord

        if not isinstance(data.get("text", ""), str):
            data["text"] = str(data.get("text", ""))

        if not isinstance(data.get("scroll_count", 0), int):
            try:
                data["scroll_count"] = int(data.get("scroll_count", 0))
            except (ValueError, TypeError):
                data["scroll_count"] = 0

        data["done"] = bool(data.get("done", False))
        data["thought"] = str(data.get("thought", ""))
        data["sent_to"] = str(data.get("sent_to", "") or "").strip()

        return data

    @staticmethod
    def _extract_json(text: str) -> Optional[str]:
        """从可能带 markdown 标记的文本中提取 JSON"""
        try:
            json.loads(text)
            return text
        except json.JSONDecodeError:
            pass
        match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
        if match:
            candidate = match.group(1).strip()
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                pass
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            candidate = match.group(0).strip()
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                pass
        return None

    # ================================================================
    #  坐标映射：截图内坐标 → 屏幕绝对坐标
    # ================================================================
    def _map_to_screen(self, coord: List[int], img: Image.Image) -> tuple:
        """
        将截图内的坐标映射为屏幕绝对坐标，并钳制到选区内。

        截图是选区的裁剪，所以：
          screen_x = rect.left + (coord_x / img_width) * rect.width
          screen_y = rect.top + (coord_y / img_height) * rect.height
        """
        if coord is None:
            # 无坐标时默认选区中心
            cx = self._rect.left + self._rect.width // 2
            cy = self._rect.top + self._rect.height // 2
            return self._rect.clamp_point(cx, cy)

        ix, iy = coord[0], coord[1]
        iw, ih = img.size
        if iw <= 0 or ih <= 0:
            return self._rect.clamp_point(self._rect.left, self._rect.top)

        # 比例映射
        sx = self._rect.left + int(ix / iw * self._rect.width)
        sy = self._rect.top + int(iy / ih * self._rect.height)

        # 钳制到选区内，绝不越界
        return self._rect.clamp_point(sx, sy)

    # ================================================================
    #  执行单个动作
    # ================================================================
    def _execute_action(self, action_dict: dict, img: Image.Image) -> ComputerUseAction:
        """执行一个动作，返回 ComputerUseAction 记录"""
        action_type = action_dict.get("action", "wait")
        thought = action_dict.get("thought", "")
        coord = action_dict.get("coordinate")
        text = action_dict.get("text", "")
        scroll_count = action_dict.get("scroll_count", 0)
        done = action_dict.get("done", False)
        sent_to = action_dict.get("sent_to", "")
        send_with_enter = action_dict.get("send_with_enter", False)

        # 任务记忆：模型标注本轮发送对象时记录，注入后续提示词防止重复/遗漏
        if sent_to:
            if sent_to not in self._sent_targets:
                self._sent_targets.append(sent_to)
                log.info(f"CU 任务记忆: 已发送名单新增 '{sent_to}' (共{len(self._sent_targets)}个)")

        record = ComputerUseAction(
            action=action_type,
            thought=thought,
            done=done,
        )

        if action_type == "done":
            log.info(f"CU 动作: done - {thought}")
            return record

        if action_type == "wait":
            log.info(f"CU 动作: wait - {thought}")
            time.sleep(random.uniform(1.0, 2.5))
            return record

        if action_type == "click":
            if coord is None:
                log.warning("CU click 缺少 coordinate，跳过")
                record.action = "wait"
                return record
            sx, sy = self._map_to_screen(coord, img)
            record.coordinate = [sx, sy]
            log.info(f"CU 动作: click({sx},{sy}) - {thought}")
            self._begin_click_through()
            try:
                _human_click(sx, sy, self._speed_min, self._speed_max,
                             self._jitter, move_mode=self._move_mode)
            finally:
                self._end_click_through()
            return record

        if action_type == "type":
            if coord is None or not text:
                log.warning("CU type 缺少 coordinate 或 text，跳过")
                record.action = "wait"
                return record
            sx, sy = self._map_to_screen(coord, img)
            record.coordinate = [sx, sy]
            record.text = text
            log.info(f"CU 动作: type({sx},{sy}) \"{text[:40]}\" - {thought}")
            self._begin_click_through()
            try:
                # 先点击定位输入框
                _human_click(sx, sy, self._speed_min, self._speed_max,
                             self._jitter, move_mode=self._move_mode)
                time.sleep(random.uniform(0.1, 0.3))
                # 输入文本
                _human_type(text, self._type_delay_min, self._type_delay_max)
                # 可选：输入完按回车发送
                if send_with_enter:
                    time.sleep(random.uniform(0.1, 0.25))
                    _press_enter()
                    log.info("CU type 已按回车发送")
            finally:
                self._end_click_through()
            return record

        if action_type == "send":
            # 按回车键发送消息（比点击发送按钮更可靠）
            log.info(f"CU 动作: send(回车) - {thought}")
            self._begin_click_through()
            try:
                _press_enter()
            finally:
                self._end_click_through()
            return record

        if action_type == "scroll":
            record.scroll_count = scroll_count
            log.info(f"CU 动作: scroll({scroll_count}) - {thought}")
            self._begin_click_through()
            try:
                # 若提供了 coordinate，先把鼠标移到目标区域再滚，
                # 否则滚轮会落在上次点击位置（可能不是要滚的列表）
                if coord is not None:
                    sx, sy = self._map_to_screen(coord, img)
                    record.coordinate = [sx, sy]
                    _human_move_to(sx, sy, self._speed_min, self._speed_max,
                                   self._jitter, mode=self._move_mode)
                    time.sleep(random.uniform(0.05, 0.15))
                _human_scroll(scroll_count)
            finally:
                self._end_click_through()
            return record

        log.warning(f"CU 未知动作类型: {action_type}")
        return record

    # ================================================================
    #  主循环：截图 → LLM → 执行 → 重复
    # ================================================================
    def run(self, capture_fn: Callable[[], Optional[Image.Image]],
            user_goal: str,
            on_state: Callable = None) -> ComputerUseResult:
        """
        执行一轮 Computer Use 循环。

        capture_fn: 截图函数，返回 PIL.Image（选区内截图）
        user_goal:  回复目标描述（如"回复对方最新的消息"）
        on_state:   状态回调（可选），每步执行前调用

        返回 ComputerUseResult
        """
        self._stop_flag = False
        actions: List[ComputerUseAction] = []
        history_dicts: List[dict] = []
        self._last_sig = None
        self._repeat_count = 0
        prev_img = None

        for step in range(self._max_steps):
            if self._stop_flag:
                log.warning(f"CU 循环在第{step}步被停止")
                return ComputerUseResult(
                    success=False,
                    actions=actions,
                    total_steps=step,
                    error="用户紧急停止",
                )

            if on_state:
                try:
                    on_state(step)
                except Exception:
                    pass

            # 步前随机停顿
            delay = random.uniform(self._step_delay_min, self._step_delay_max)
            log.debug(f"CU 第{step}步，步前停顿 {delay:.2f}s")
            time.sleep(delay)

            # 截图
            img = capture_fn()
            if img is None:
                log.error(f"CU 第{step}步截图失败，终止循环")
                return ComputerUseResult(
                    success=False,
                    actions=actions,
                    total_steps=step,
                    error="截图失败",
                )

            # 动作效果检测：与上一帧对比，若无变化则提醒 LLM
            no_effect = False
            if prev_img is not None and self._images_similar(prev_img, img):
                no_effect = True
                log.warning(f"CU 第{step}步检测到画面无变化，上一步动作可能未命中目标")
            prev_img = img.copy()

            # 调用 LLM
            img_b64 = self._image_to_base64(img)
            action_dict = self._call_llm(img_b64, user_goal, history_dicts,
                                         no_effect=no_effect)
            if action_dict is None:
                log.error(f"CU 第{step}步 LLM 调用失败，终止循环")
                return ComputerUseResult(
                    success=False,
                    actions=actions,
                    total_steps=step,
                    error=f"第{step}步 LLM 调用失败",
                )

            # 执行动作
            record = self._execute_action(action_dict, img)
            actions.append(record)

            # 循环检测：连续相同动作无进展则中断，避免空转浪费
            sig = (
                record.action,
                tuple(record.coordinate) if record.coordinate else None,
                record.scroll_count,
                record.text,
            )
            if sig == self._last_sig:
                self._repeat_count += 1
            else:
                self._last_sig = sig
                self._repeat_count = 1
            if self._repeat_count >= 3:
                log.warning(
                    f"CU 第{step}步检测到动作连续重复{self._repeat_count}次无进展，"
                    f"强制终止。动作: {record.action} 坐标: {record.coordinate}"
                )
                return ComputerUseResult(
                    success=False,
                    actions=actions,
                    total_steps=step + 1,
                    error=f"动作连续重复{self._repeat_count}次无进展，可能点击未命中目标",
                )

            # 记录历史（供下一步 LLM 参考）
            history_dicts.append({
                "action": record.action,
                "thought": record.thought,
                "coordinate": action_dict.get("coordinate"),
                "text": record.text[:50] if record.text else "",
                "scroll_count": record.scroll_count,
                "done": record.done,
            })

            # 判断完成
            if record.done or action_dict.get("action") == "done":
                log.info(f"CU 循环完成，共 {step + 1} 步")
                return ComputerUseResult(
                    success=True,
                    actions=actions,
                    total_steps=step + 1,
                )

            # 等待画面刷新
            time.sleep(self._screenshot_wait)

        # 达到最大步数
        log.warning(f"CU 达到最大步数({self._max_steps})，强制停止")
        return ComputerUseResult(
            success=False,
            actions=actions,
            total_steps=self._max_steps,
            error=f"达到最大步数 {self._max_steps}",
        )
