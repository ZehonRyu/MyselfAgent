"""
画面采集模块
=================
职责：
1. 单帧截图：截取选区范围
2. 定时快照模式：随机长间隔循环截图，用于消息监控
3. 历史采集模式：滚轮向上翻页 + 逐页截图 + 内容重复检测终止

依赖：
- mss: 快速屏幕截图
- imagehash: 图像感知哈希，用于翻页终止检测
- ctypes + SendInput: 滚轮模拟（需在选区内执行）
"""
import time
import random
import ctypes
import threading
from typing import Callable, List, Optional
from PIL import Image
import mss
import imagehash

from .models import Rect
from .logger import log

# SendInput 常量
INPUT_MOUSE = 0
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class INPUT(ctypes.Structure):
    class _INPUT(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]
    _anonymous_ = ("_input",)
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("_input", _INPUT),
    ]


def send_mouse_wheel(delta: int):
    """发送鼠标滚轮事件，delta>0 向上滚动"""
    extra = ctypes.c_ulong(0)
    mi = MOUSEINPUT(
        dx=0, dy=0,
        mouseData=delta,
        dwFlags=MOUSEEVENTF_WHEEL,
        time=0,
        dwExtraInfo=ctypes.addressof(extra),
    )
    inp = INPUT(type=INPUT_MOUSE)
    inp._input.mi = mi  # type: ignore
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


class CaptureModule:
    """画面采集模块

    rect_provider: 从 Overlay 获取当前选区坐标的回调
    overlay_callbacks: 截图前后控制 Overlay 蒙版的回调
    """

    def __init__(
        self,
        rect_provider: Callable[[], Rect],
        on_enter_capture: Callable = None,
        on_exit_capture: Callable = None,
    ):
        self._get_rect = rect_provider
        self._on_enter_capture = on_enter_capture
        self._on_exit_capture = on_exit_capture
        self._sct = mss.mss()

        # 定时快照线程控制
        self._periodic_thread: threading.Thread = None
        self._periodic_running = False
        self._periodic_stop_event = threading.Event()

        # 历史采集控制
        self._history_running = False
        self._history_stop_event = threading.Event()

        # 紧急停止
        self._emergency_stop = False

    def stop_all(self):
        """紧急停止：停止所有采集循环"""
        self._emergency_stop = True
        self._periodic_stop_event.set()
        self._history_stop_event.set()
        self._periodic_running = False
        self._history_running = False

    def reset_emergency(self):
        self._emergency_stop = False
        self._periodic_stop_event.clear()
        self._history_stop_event.clear()

    # ================================================================
    #  单帧截图
    # ================================================================
    def capture_single(self) -> Optional[Image.Image]:
        """截取选区内单帧画面"""
        rect = self._get_rect()
        if rect is None or rect.width < 10 or rect.height < 10:
            log.warning("选区无效，跳过截图")
            return None

        # 临时隐藏 Overlay 蒙版
        if self._on_enter_capture:
            self._on_enter_capture()
            time.sleep(0.15)  # 等待主线程处理信号并刷新蒙版

        try:
            monitor = {
                "left": rect.left,
                "top": rect.top,
                "width": rect.width,
                "height": rect.height,
            }
            shot = self._sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            log.debug(f"截图完成: {rect.width}x{rect.height}")
            return img
        except Exception as e:
            log.error(f"截图失败: {e}")
            return None
        finally:
            if self._on_exit_capture:
                self._on_exit_capture()

    # ================================================================
    #  全屏截图（用于导航：在联系人列表里找目标联系人）
    # ================================================================
    def capture_full_desktop(self):
        """截取整个虚拟桌面（全部显示器合并），返回 (PIL.Image, (origin_x, origin_y))

        origin 为虚拟桌面左上角在屏幕坐标系下的偏移（多屏时可能为负）。
        图像像素坐标与屏幕坐标 1:1 对应（DPI 感知已开启）。
        """
        try:
            mon = self._sct.monitors[0]  # 虚拟桌面（所有屏合并）
            shot = self._sct.grab(mon)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            return img, (mon["left"], mon["top"])
        except Exception as e:
            log.error(f"全屏截图失败: {e}")
            return None

    # ================================================================
    #  定时快照模式
    # ================================================================
    def start_periodic(
        self,
        interval_range: tuple,
        callback: Callable[[Image.Image], None],
    ):
        """启动定时快照

        interval_range: (min_sec, max_sec) 随机间隔
        callback: 截图完成回调，接收 PIL.Image
        """
        if self._periodic_running:
            log.warning("定时快照已在运行")
            return
        self._periodic_running = True
        self._periodic_stop_event.clear()
        self._periodic_thread = threading.Thread(
            target=self._periodic_loop,
            args=(interval_range, callback),
            daemon=True,
        )
        self._periodic_thread.start()
        log.info(f"定时快照启动，间隔范围 {interval_range[0]}-{interval_range[1]}s")

    def stop_periodic(self):
        """停止定时快照"""
        self._periodic_stop_event.set()
        self._periodic_running = False
        log.info("定时快照已停止")

    def _periodic_loop(self, interval_range, callback):
        min_sec, max_sec = interval_range
        while not self._periodic_stop_event.is_set() and not self._emergency_stop:
            # 截图
            img = self.capture_single()
            if img is not None:
                try:
                    callback(img)
                except Exception as e:
                    log.error(f"快照回调异常: {e}")

            # 随机长间隔等待
            wait = random.uniform(min_sec, max_sec)
            log.debug(f"下次快照等待 {wait:.1f}s")
            # 分段等待，便于快速响应停止
            self._periodic_stop_event.wait(timeout=wait)

    # ================================================================
    #  历史采集模式
    # ================================================================
    def collect_history(
        self,
        scroll_steps: int = 5,
        scroll_wait: float = 0.8,
        similarity_threshold: float = 0.92,
        max_pages: int = 100,
        page_callback: Callable[[Image.Image, int], None] = None,
    ) -> List[Image.Image]:
        """
        历史采集：滚轮向上翻页 + 逐页截图

        终止条件：
        1. 连续两页图像相似度 > similarity_threshold（到顶）
        2. 达到 max_pages 保护上限
        3. 紧急停止

        返回所有截图列表（从最新到最旧）
        """
        self._history_running = True
        self._history_stop_event.clear()
        images: List[Image.Image] = []
        prev_hash: Optional[imagehash.ImageHash] = None
        repeat_count = 0

        log.info("历史采集开始")

        for page in range(max_pages):
            if self._history_stop_event.is_set() or self._emergency_stop:
                log.info("历史采集被终止")
                break

            # 截图当前页
            img = self.capture_single()
            if img is None:
                log.warning(f"第{page}页截图失败，跳过")
                continue

            images.append(img)
            curr_hash = imagehash.phash(img)

            # 相似度检测
            if prev_hash is not None:
                # 汉明距离归一化为相似度（0~1）
                max_bits = len(curr_hash.hash) ** 2
                distance = curr_hash - prev_hash
                similarity = 1.0 - (distance / max_bits)
                log.debug(f"第{page}页相似度: {similarity:.3f}")

                if similarity > similarity_threshold:
                    repeat_count += 1
                    if repeat_count >= 1:
                        log.info(f"第{page}页与前一页高度相似(sim={similarity:.3f})，判定到顶")
                        break
                else:
                    repeat_count = 0

            # 回调
            if page_callback:
                try:
                    page_callback(img, page)
                except Exception as e:
                    log.error(f"页回调异常: {e}")

            prev_hash = curr_hash

            # 向上滚动翻页
            for _ in range(scroll_steps):
                send_mouse_wheel(120)  # +120 = 向上滚
                time.sleep(0.05)
            # 等待渲染
            time.sleep(scroll_wait + random.uniform(0.1, 0.3))

        self._history_running = False
        log.info(f"历史采集完成，共 {len(images)} 页")
        return images

    def stop_history(self):
        """停止历史采集"""
        self._history_stop_event.set()
        self._history_running = False
        log.info("历史采集停止信号已发送")
