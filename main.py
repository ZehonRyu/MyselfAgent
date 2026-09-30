"""
聊天辅助工具 - 主入口
==========================
串联所有模块：Overlay选区 + 控制面板 + 画面采集 + VL识别 + AI Agent + Computer Use Agent。

支持两种运行模式：
1. vl_agent：定时快照 → VL识别消息 → AI Agent生成回复 → 键鼠发送（两段式）
2. computer_use：截图 → 多模态LLM输出动作 → SendInput执行 → 循环（通用 GUI Agent）

已实现：
1. Overlay 悬浮选区（多屏支持、蒙版穿透）
2. 控制面板（状态/按钮/配置/日志/快捷键/模式切换）
3. 画面采集（定时快照 + 历史采集）
4. VL 视觉识别（OpenRouter 多模态）
5. AI Agent 回复生成（OpenRouter）
6. Computer Use Agent（截图→LLM→动作→键鼠执行 循环，通用聊天软件）

运行：python main.py
"""
import sys
import os
import ctypes
import random
import time

# --- DPI 感知（必须在 QApplication 创建前调用）---
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt, QObject, pyqtSignal, QThread

from config import Config
from modules.models import Rect, AppState, Message
from modules.logger import log, Logger
from modules.overlay import OverlayWindow
from modules.control_panel import ControlPanel
from modules.capture import CaptureModule
from modules.vl_recognition import VLRecognition
from modules.ai_agent import AIAgent
from modules.computer_use_agent import (
    ComputerUseAgent,
    _human_click,
    _human_type,
    _press_enter,
)


class AppController(QObject):
    """
    应用控制器
    串联所有模块，充当简易调度器。
    """

    # 工作线程 → 主线程的 GUI 更新信号
    _update_state_sig = pyqtSignal(object)       # AppState
    _update_msg_sig = pyqtSignal(str, str)        # sender, text
    _append_log_sig = pyqtSignal(str)             # message

    def __init__(self, config: Config):
        super().__init__()
        self._config = config

        # --- 创建 Overlay ---
        self._overlay = OverlayWindow()

        # --- 创建采集模块 ---
        self._capture = CaptureModule(
            rect_provider=self._overlay.get_current_rect,
            on_enter_capture=self._overlay.enter_capture_mode,
            on_exit_capture=self._overlay.exit_capture_mode,
        )

        # --- 创建 VL 识别模块 ---
        self._vl = VLRecognition(
            api_url=config.vl_api_url,
            api_key=config.vl_api_key,
            model_name=config.vl_model_name,
            timeout=config.vl_timeout,
            provider=config.vl_provider,
        )

        # --- 创建 AI Agent 模块 ---
        self._agent = AIAgent(
            api_url=config.agent_api_url,
            api_key=config.agent_api_key,
            model_name=config.agent_model_name,
            timeout=config.agent_timeout,
            provider=config.agent_provider,
        )

        # --- 创建 Computer Use Agent 模块（通用聊天软件操作）---
        self._cu_agent = ComputerUseAgent(
            api_url=config.vl_api_url,      # CU 复用 VL 的多模态模型
            api_key=config.vl_api_key,
            model_name=config.vl_model_name,
            timeout=config.vl_timeout,
            provider=config.vl_provider,
            rect=self._overlay.get_current_rect(),
        )
        self._cu_agent.set_human_params(
            speed_min=config.mouse_speed_min,
            speed_max=config.mouse_speed_max,
            jitter=config.mouse_jitter,
            type_delay_min=config.typing_delay_min,
            type_delay_max=config.typing_delay_max,
            step_delay_min=config.cu_step_delay_min,
            step_delay_max=config.cu_step_delay_max,
            screenshot_wait=config.cu_screenshot_interval,
            max_steps=config.cu_max_steps,
        )
        self._cu_agent.set_move_mode(config.mouse_move_mode)
        # CU 动作执行期间让 Overlay 点击穿透（OS 级样式切换 + 清除蒙版双保险）
        self._cu_agent.set_click_through(
            self._overlay.enter_capture_mode,
            self._overlay.exit_capture_mode,
        )
        self._cu_agent.set_overlay_hwnd(int(self._overlay.winId()))

        # --- 创建控制面板 ---
        self._panel = ControlPanel(config)
        self._panel.selection_mode_requested.connect(self._on_selection_mode)
        self._panel.emergency_stop_requested.connect(self._on_emergency_stop)
        self._panel.history_collect_requested.connect(self._on_history_collect)
        self._panel.start_pause_requested.connect(self._on_start_pause)
        self._panel.config_saved.connect(self._on_config_saved)
        self._panel.test_api_requested.connect(self._on_test_api)
        self._panel.cu_goal_changed.connect(self._on_cu_goal_changed)

        # --- 信号连接：工作线程 → 主线程 GUI ---
        self._update_state_sig.connect(self._panel.update_state)
        self._update_msg_sig.connect(self._panel.update_last_message)

        # --- 当前选区缓存 ---
        self._current_rect: Rect = self._overlay.get_current_rect()

        # --- 选区持久化：恢复上次保存的选区位置 ---
        if config.selection_rect and len(config.selection_rect) == 4:
            try:
                l, t, w, h = config.selection_rect
                if w >= 50 and h >= 50:
                    self._overlay.set_rect(Rect(left=l, top=t, width=w, height=h))
                    self._current_rect = self._overlay.get_current_rect()
                    log.info(f"已恢复上次选区: ({l},{t}) {w}x{h}")
            except Exception as e:
                log.warning(f"恢复选区失败，使用默认位置: {e}")

        self._overlay.rect_changed.connect(self._on_rect_changed)

        # --- 自动化运行状态 ---
        self._auto_running = False
        self._cu_thread: ComputerUseThread = None
        self._nav_done = False  # 联系人导航是否已完成（每轮启动后执行一次）

        # --- 消息上下文缓存（简易版，后续独立为 context_manager）---
        self._message_history: list[Message] = []

        # --- 风控计数 ---
        self._reply_count_this_hour = 0
        self._hour_start_time = time.time()

        log.info("应用初始化完成（含 VL + Agent + Computer Use 模块）")

    # ================================================================
    #  启动
    # ================================================================
    def start(self):
        self._overlay.show()
        self._overlay.set_locked(False)
        self._panel.show()
        self._panel.update_state(AppState.IDLE)
        log.info("Overlay 和控制面板已显示")

    # ================================================================
    #  选区操作
    # ================================================================
    def _on_rect_changed(self, rect: Rect):
        self._current_rect = rect
        self._cu_agent.set_rect(rect)
        log.debug(f"选区更新: ({rect.left},{rect.top}) {rect.width}x{rect.height}")

    def _on_selection_mode(self):
        self._overlay.set_locked(False)
        self._overlay.raise_()
        self._overlay.activateWindow()
        log.info("进入选区设置模式，请拖拽/缩放红色选区框")

    # ================================================================
    #  历史采集
    # ================================================================
    def _on_history_collect(self):
        if self._capture._history_running:
            log.warning("历史采集已在进行中")
            return

        c = self._config
        self._panel.update_state(AppState.HISTORY_COLLECTING)
        log.info("=" * 50)
        log.info("开始历史聊天记录采集")
        log.info(f"参数: 滚轮步数={c.history_scroll_steps}, "
                 f"等待={c.history_scroll_wait}s, "
                 f"相似度阈值={c.history_similarity_threshold}")

        self._history_thread = HistoryCollectThread(
            self._capture,
            scroll_steps=c.history_scroll_steps,
            scroll_wait=c.history_scroll_wait,
            similarity_threshold=c.history_similarity_threshold,
            max_pages=c.history_max_pages,
            screenshot_path=c.screenshot_path,
        )
        self._history_thread.finished_with_count.connect(self._on_history_finished)
        self._history_thread.start()

    def _on_history_finished(self, page_count: int):
        log.info(f"历史采集完成，共 {page_count} 页")
        self._panel.update_state(AppState.IDLE)

    # ================================================================
    #  启动/暂停
    # ================================================================
    def _on_start_pause(self):
        if self._auto_running:
            self._auto_running = False
            self._capture.stop_periodic()
            self._cu_agent.stop()
            self._panel.update_state(AppState.PAUSED)
            log.info("自动化已暂停")
        else:
            rect = self._current_rect
            if rect.width < 50 or rect.height < 50:
                log.error("选区太小，请先设置选区")
                self._panel.reset_start_button()
                return

            c = self._config
            if not c.vl_api_key:
                log.error("VL API Key 未配置，请在控制面板设置")
                self._panel.reset_start_button()
                return

            # Computer Use 模式只需 VL 多模态模型
            if c.run_mode == "vl_agent" and not c.agent_api_key:
                log.error("Agent API Key 未配置，请在控制面板设置")
                self._panel.reset_start_button()
                return

            self._auto_running = True
            self._overlay.set_locked(True)
            self._panel.update_state(AppState.IDLE)
            self._nav_done = False  # 每次启动后重新导航一次

            # 持久化当前选区，重启后自动恢复
            try:
                r = self._current_rect
                self._config.selection_rect = [r.left, r.top, r.width, r.height]
                self._config.save()
            except Exception as e:
                log.warning(f"选区持久化失败: {e}")

            if c.run_mode == "computer_use":
                # --- Computer Use 模式：启动 CU 工作线程 ---
                log.info("自动化已启动 [Computer Use 模式]：截图→LLM→动作→执行 循环")
                self._cu_agent.set_rect(self._current_rect)
                self._cu_agent.reset_stop()
                self._cu_agent.reset_task_memory()
                self._cu_thread = ComputerUseThread(
                    cu_agent=self._cu_agent,
                    capture_fn=self._capture.capture_single,
                    user_goal=c.cu_goal or "观察当前屏幕，根据需要执行操作；如果没有需要处理的事情就直接完成。",
                    reply_limit=c.reply_limit_per_hour,
                    poll_interval_range=(c.poll_interval_min, c.poll_interval_max),
                )
                self._cu_thread.finished_with_result.connect(self._on_cu_finished)
                self._cu_thread.start()
            else:
                # --- VL + Agent 模式：定时快照 ---
                log.info("自动化已启动 [VL+Agent 模式]：定时快照 → VL识别 → Agent回复")
                self._capture.start_periodic(
                    interval_range=(c.poll_interval_min, c.poll_interval_max),
                    callback=self._on_snapshot,
                )

    def _on_cu_finished(self, success: bool, steps: int, error: str):
        """Computer Use 工作线程完成回调"""
        self._auto_running = False
        self._overlay.set_locked(False)
        if success:
            log.info(f"Computer Use 循环完成，共 {steps} 步")
            self._panel.update_state(AppState.IDLE)
        else:
            log.warning(f"Computer Use 循环结束: {error}")
            self._panel.update_state(AppState.PAUSED)
        self._panel.reset_start_button()

    # ================================================================
    #  定时快照 → VL → Agent 主链路
    # ================================================================
    def _on_snapshot(self, img):
        """
        定时快照回调（在工作线程中执行）
        链路：截图 → VL识别 → 判断新消息 → Agent生成回复 → (键鼠执行待实现)
        """
        if not self._auto_running:
            return

        # --- 联系人导航：按提示词点击目标联系人（每轮启动后执行一次）---
        if self._config.cu_goal and not self._nav_done:
            self._nav_done = True
            self._update_state_sig.emit(AppState.CAPTURING)
            log.info(f"导航到目标联系人: {self._config.cu_goal}")
            nav_ok = self._navigate_to_contact(self._config.cu_goal)
            if nav_ok:
                # 等待目标聊天窗口加载，然后重新截图（选区此时应为目标聊天）
                time.sleep(1.2)
                img = self._capture.capture_single()
                if img is None:
                    log.warning("导航后重新截图失败，跳过本轮")
                    return

        # --- VL 识别 ---
        self._update_state_sig.emit(AppState.CAPTURING)
        self._update_state_sig.emit(AppState.VL_RECOGNIZING)
        vl_result = self._vl.recognize(img)

        if not vl_result.is_valid:
            log.error(f"VL识别失败，暂停自动化: {vl_result.error}")
            self._auto_running = False
            self._capture.stop_periodic()
            self._update_state_sig.emit(AppState.PAUSED)
            self._append_log_sig.emit(f"VL识别失败: {vl_result.error}，自动化已暂停")
            return

        # 更新消息历史
        if vl_result.messages:
            self._message_history = vl_result.messages[-self._config.max_context_messages:]

        # 更新最近消息显示
        if vl_result.messages:
            last_msg = vl_result.messages[-1]
            self._update_msg_sig.emit(last_msg.sender, last_msg.text)

        # --- 判断是否有新消息需要回复 ---
        if not vl_result.has_new_message:
            self._update_state_sig.emit(AppState.IDLE)
            log.info("无新消息，继续等待")
            return

        # --- 风控检查：每小时回复上限 ---
        self._check_reply_limit()
        if self._reply_count_this_hour >= self._config.reply_limit_per_hour:
            log.warning(f"已达每小时回复上限({self._config.reply_limit_per_hour})，暂停自动化")
            self._auto_running = False
            self._capture.stop_periodic()
            self._update_state_sig.emit(AppState.PAUSED)
            return

        # --- AI Agent 生成回复 ---
        self._update_state_sig.emit(AppState.WAITING_AGENT)
        agent_result = self._agent.generate_reply(self._message_history)

        if not agent_result.success:
            log.error(f"Agent调用失败，暂停自动化: {agent_result.error}")
            self._auto_running = False
            self._capture.stop_periodic()
            self._update_state_sig.emit(AppState.PAUSED)
            return

        # --- 思考等待（风控约束：随机长等待）---
        wait = random.uniform(self._config.think_wait_min, self._config.think_wait_max)
        log.info(f"Agent回复: {agent_result.reply_text}")
        log.info(f"插入思考等待 {wait:.1f}s 后执行键鼠操作")
        self._update_state_sig.emit(AppState.TYPING)
        time.sleep(wait)

        # --- 键鼠执行：点击输入框 → 输入回复 → 回车发送 ---
        sent = self._send_reply(agent_result.reply_text, vl_result.input_box, img)
        if sent:
            self._reply_count_this_hour += 1
        self._update_state_sig.emit(AppState.IDLE)

    def _send_reply(self, reply_text: str, input_box, img) -> bool:
        """键鼠发送回复：进入点击穿透 → 点击输入框 → 打字 → 回车 → 退出穿透

        操作前让 Overlay 进入 capture 模式（清除 mask），点击穿透到底层聊天窗口，
        避免悬浮窗的选区 mask 拦截点击。操作结束恢复 mask。
        """
        if not input_box or img is None:
            log.warning("未识别到输入框(input_box)，跳过键鼠发送")
            self._append_log_sig.emit("未识别到输入框，回复未发送")
            return False
        try:
            rect = self._overlay.get_current_rect()
            iw, ih = img.size
            # 输入框中心点（图像坐标）按比例映射到屏幕绝对坐标
            cx_img = input_box.left + input_box.width / 2
            cy_img = input_box.top + input_box.height / 2
            sx = rect.left + int(cx_img / iw * rect.width)
            sy = rect.top + int(cy_img / ih * rect.height)
            sx, sy = rect.clamp_point(sx, sy)

            log.info(f"键鼠发送: 点击输入框({sx},{sy}) 输入'{reply_text[:30]}' 回车")
            self._append_log_sig.emit(f"发送回复: {reply_text}")
            # 进入点击穿透模式，让点击落到底层聊天窗口
            self._overlay.enter_capture_mode()
            time.sleep(0.2)  # 等待主线程清除 mask
            try:
                _human_click(sx, sy,
                             self._config.mouse_speed_min,
                             self._config.mouse_speed_max,
                             self._config.mouse_jitter,
                             move_mode=self._config.mouse_move_mode)
                time.sleep(random.uniform(0.15, 0.35))  # 等输入框聚焦
                _human_type(reply_text,
                            self._config.typing_delay_min,
                            self._config.typing_delay_max)
                time.sleep(random.uniform(0.1, 0.25))
                _press_enter()
            finally:
                self._overlay.exit_capture_mode()
            return True
        except Exception as e:
            log.error(f"键鼠发送异常: {e}")
            self._append_log_sig.emit(f"键鼠发送失败: {e}")
            try:
                self._overlay.exit_capture_mode()
            except Exception:
                pass
            return False

    def _navigate_to_contact(self, goal: str) -> bool:
        """导航：全屏截图 → VL 找目标联系人 → 点击，打开其聊天窗口

        全屏截图坐标与屏幕 1:1（DPI 感知），故 target 坐标 + 虚拟桌面 origin
        即为屏幕绝对坐标。点击前进 capture 模式确保穿透到聊天窗口。
        """
        full = self._capture.capture_full_desktop()
        if not full:
            log.warning("全屏截图失败，跳过导航")
            return False
        img, (ox, oy) = full
        target = self._vl.find_contact(img, goal)
        if target is None:
            log.warning(f"未找到目标联系人'{goal}'，回复当前聊天")
            self._append_log_sig.emit(f"未找到'{goal}'，回复当前聊天")
            return False
        try:
            # 图像坐标 1:1 映射到屏幕绝对坐标
            cx = target.left + target.width // 2
            cy = target.top + target.height // 2
            sx = ox + cx
            sy = oy + cy
            log.info(f"导航点击联系人'{goal}': ({sx},{sy})")
            self._append_log_sig.emit(f"点击联系人: {goal}")
            self._overlay.enter_capture_mode()
            time.sleep(0.2)  # 等待主线程清除 mask
            try:
                _human_click(sx, sy,
                             self._config.mouse_speed_min,
                             self._config.mouse_speed_max,
                             self._config.mouse_jitter,
                             move_mode=self._config.mouse_move_mode)
            finally:
                self._overlay.exit_capture_mode()
            return True
        except Exception as e:
            log.error(f"导航点击异常: {e}")
            try:
                self._overlay.exit_capture_mode()
            except Exception:
                pass
            return False

    def _check_reply_limit(self):
        """检查并重置每小时回复计数"""
        now = time.time()
        if now - self._hour_start_time >= 3600:
            self._reply_count_this_hour = 0
            self._hour_start_time = now

    # ================================================================
    #  API 连通性测试
    # ================================================================
    def _on_test_api(self):
        """在后台线程测试 VL/Agent API 连通性"""
        log.info("开始测试 API 连通性...")
        self._panel.update_state(AppState.WAITING_AGENT)

        self._test_thread = ApiTestThread(
            vl=self._vl,
            agent=self._agent,
        )
        self._test_thread.finished_with_result.connect(self._on_test_finished)
        self._test_thread.start()

    def _on_test_finished(self, vl_ok: bool, agent_ok: bool, detail: str):
        """API 测试完成回调"""
        self._panel.update_state(AppState.IDLE)
        if vl_ok and agent_ok:
            log.info(f"API 测试通过: {detail}")
        else:
            log.error(f"API 测试失败: {detail}")
        log.info(f"  VL API: {'OK' if vl_ok else 'FAIL'}")
        log.info(f"  Agent API: {'OK' if agent_ok else 'FAIL'}")

    # ================================================================
    #  CU 提示词实时更新
    # ================================================================
    def _on_cu_goal_changed(self, goal: str):
        """运行中修改 CU 提示词，下一轮循环生效"""
        if goal:
            log.info(f"CU 提示词已更新: {goal[:50]}...")
        if self._cu_thread is not None and self._cu_thread.isRunning():
            self._cu_thread.update_goal(goal)

    # ================================================================
    #  紧急停止
    # ================================================================
    def _on_emergency_stop(self):
        self._auto_running = False
        self._capture.stop_all()
        self._capture.reset_emergency()
        self._cu_agent.stop()
        if self._cu_thread is not None and self._cu_thread.isRunning():
            self._cu_thread.quit()
        self._overlay.set_locked(False)
        self._panel.update_state(AppState.EMERGENCY_STOPPED)
        self._panel.reset_start_button()
        log.warning("紧急终止：所有自动化已停止")

    # ================================================================
    #  配置更新
    # ================================================================
    def _on_config_saved(self, config: Config):
        self._config = config
        log.update_config(log_path=config.log_path, level=config.log_level)

        # 更新 VL 配置
        self._vl.update_config(
            api_url=config.vl_api_url,
            api_key=config.vl_api_key,
            model_name=config.vl_model_name,
            timeout=config.vl_timeout,
            provider=config.vl_provider,
        )

        # 更新 Agent 配置
        self._agent.update_config(
            api_url=config.agent_api_url,
            api_key=config.agent_api_key,
            model_name=config.agent_model_name,
            timeout=config.agent_timeout,
            provider=config.agent_provider,
        )

        # 更新 Computer Use Agent 配置（复用 VL 多模态模型）
        self._cu_agent.update_config(
            api_url=config.vl_api_url,
            api_key=config.vl_api_key,
            model_name=config.vl_model_name,
            timeout=config.vl_timeout,
            provider=config.vl_provider,
        )
        self._cu_agent.set_human_params(
            speed_min=config.mouse_speed_min,
            speed_max=config.mouse_speed_max,
            jitter=config.mouse_jitter,
            type_delay_min=config.typing_delay_min,
            type_delay_max=config.typing_delay_max,
            step_delay_min=config.cu_step_delay_min,
            step_delay_max=config.cu_step_delay_max,
            screenshot_wait=config.cu_screenshot_interval,
            max_steps=config.cu_max_steps,
        )
        self._cu_agent.set_move_mode(config.mouse_move_mode)
        # CU 动作执行期间让 Overlay 点击穿透（OS 级样式切换 + 清除蒙版双保险）
        self._cu_agent.set_click_through(
            self._overlay.enter_capture_mode,
            self._overlay.exit_capture_mode,
        )
        self._cu_agent.set_overlay_hwnd(int(self._overlay.winId()))

        log.info("配置已更新（VL + Agent + Computer Use）")


class HistoryCollectThread(QThread):
    """历史采集工作线程"""
    finished_with_count = pyqtSignal(int)

    def __init__(self, capture: CaptureModule, scroll_steps: int,
                 scroll_wait: float, similarity_threshold: float,
                 max_pages: int, screenshot_path: str):
        super().__init__()
        self._capture = capture
        self._params = {
            "scroll_steps": scroll_steps,
            "scroll_wait": scroll_wait,
            "similarity_threshold": similarity_threshold,
            "max_pages": max_pages,
        }
        self._screenshot_path = screenshot_path
        self._page_count = 0

    def run(self):
        def page_cb(img, page_num):
            self._page_count = page_num + 1
            os.makedirs(self._screenshot_path, exist_ok=True)
            img_path = os.path.join(
                self._screenshot_path,
                f"history_{page_num:04d}.png"
            )
            try:
                img.save(img_path)
                log.info(f"第{page_num}页已保存: {img_path}")
            except Exception as e:
                log.error(f"保存截图失败: {e}")

        images = self._capture.collect_history(
            page_callback=page_cb,
            **self._params,
        )
        self._page_count = len(images)
        self.finished_with_count.emit(self._page_count)


class ApiTestThread(QThread):
    """API 连通性测试线程"""
    finished_with_result = pyqtSignal(bool, bool, str)  # vl_ok, agent_ok, detail

    def __init__(self, vl, agent, parent=None):
        super().__init__(parent)
        self._vl = vl
        self._agent = agent

    def run(self):
        vl_ok = False
        agent_ok = False
        detail_parts = []

        # 测试 VL API（发一条纯文本请求）
        try:
            import requests
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._vl._api_key}",
            }
            if self._vl._provider == "openrouter":
                headers["HTTP-Referer"] = "http://localhost"
                headers["X-Title"] = "ChatAssistantTest"
            payload = {
                "model": self._vl._model,
                "messages": [{"role": "user", "content": "回复OK"}],
                "max_tokens": 10,
            }
            resp = requests.post(
                self._vl._api_url, headers=headers,
                json=payload, timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                reply = data["choices"][0]["message"]["content"]
                vl_ok = True
                detail_parts.append(f"VL({self._vl._model}): OK, 返回='{reply[:30]}'")
            else:
                detail_parts.append(f"VL HTTP {resp.status_code}: {resp.text[:150]}")
        except Exception as e:
            detail_parts.append(f"VL 异常: {e}")

        # 测试 Agent API
        try:
            import requests
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._agent._api_key}",
            }
            if self._agent._provider == "openrouter":
                headers["HTTP-Referer"] = "http://localhost"
                headers["X-Title"] = "ChatAssistantTest"
            payload = {
                "model": self._agent._model,
                "messages": [{"role": "user", "content": "回复OK"}],
                "max_tokens": 10,
            }
            resp = requests.post(
                self._agent._api_url, headers=headers,
                json=payload, timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                reply = data["choices"][0]["message"]["content"]
                agent_ok = True
                detail_parts.append(f"Agent({self._agent._model}): OK, 返回='{reply[:30]}'")
            else:
                detail_parts.append(f"Agent HTTP {resp.status_code}: {resp.text[:150]}")
        except Exception as e:
            detail_parts.append(f"Agent 异常: {e}")

        self.finished_with_result.emit(vl_ok, agent_ok, " | ".join(detail_parts))


class ComputerUseThread(QThread):
    """
    Computer Use 工作线程

    在后台线程中运行 ComputerUseAgent.run()，
    带风控：每小时回复上限 + 随机长间隔轮询。
    完成后通过 finished_with_result 信号通知主线程（线程安全）。
    """
    finished_with_result = pyqtSignal(bool, int, str)  # success, steps, error

    def __init__(self, cu_agent: ComputerUseAgent,
                 capture_fn, user_goal: str,
                 reply_limit: int,
                 poll_interval_range: tuple,
                 parent=None):
        super().__init__(parent)
        self._cu_agent = cu_agent
        self._capture_fn = capture_fn
        self._user_goal = user_goal  # 可运行中通过 update_goal 修改
        self._reply_limit = reply_limit
        self._poll_min, self._poll_max = poll_interval_range
        self._reply_count = 0
        self._hour_start = time.time()

    def update_goal(self, goal: str):
        """运行中更新提示词，下一轮循环生效"""
        if goal:
            self._user_goal = goal

    def run(self):
        while not self._cu_agent._stop_flag:
            # 检查每小时回复上限
            self._check_hour_reset()
            if self._reply_count >= self._reply_limit:
                log.warning(f"Computer Use 达到每小时回复上限({self._reply_limit})，停止")
                break

            # 执行一轮 Computer Use 循环（每次读取最新的 user_goal）
            result = self._cu_agent.run(
                capture_fn=self._capture_fn,
                user_goal=self._user_goal,
            )

            if result.success and result.actions:
                self._reply_count += 1
                log.info(f"Computer Use 一轮完成({result.total_steps}步)，"
                         f"本小时已回复 {self._reply_count}/{self._reply_limit}")
            else:
                log.info(f"Computer Use 一轮结束: {result.error or '无新消息'}")

            if self._cu_agent._stop_flag:
                break

            # 随机长间隔等待下次检查
            wait = random.uniform(self._poll_min, self._poll_max)
            log.info(f"Computer Use 等待 {wait:.1f}s 后进行下一轮检查")
            # 分段等待，便于响应停止
            end_time = time.time() + wait
            while time.time() < end_time and not self._cu_agent._stop_flag:
                time.sleep(0.5)

        success = True
        steps = self._reply_count
        error = ""
        self.finished_with_result.emit(success, steps, error)

    def _check_hour_reset(self):
        now = time.time()
        if now - self._hour_start >= 3600:
            self._reply_count = 0
            self._hour_start = now


def main():
    app = QApplication(sys.argv)
    config = Config.load()
    app.setStyle("Fusion")

    controller = AppController(config)
    controller.start()

    ret = app.exec_()
    log.info("应用退出")
    sys.exit(ret)


if __name__ == "__main__":
    main()
