"""
控制面板模块
=================
用户操作界面，包含：
1. 状态显示区：当前运行状态 + 最近识别消息
2. 操作按钮区：启动/暂停、历史采集、紧急终止、设置选区
3. 模式选择区：VL+Agent 两段式 / Computer Use 循环
4. 配置区：轮询间隔、回复上限、VL/Agent地址、CU参数、存储路径
5. 日志输出区：实时滚动日志

对外信号（由 main.py 连接到调度器）：
- start_pause_requested()
- history_collect_requested()
- emergency_stop_requested()
- selection_mode_requested()
- config_saved(Config)

对内接收（由调度器推送）：
- update_state(AppState)
- update_last_message(str, str)  # sender, text
- append_log(str, str)  # level, message
"""
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QLabel, QPushButton, QTextEdit, QLineEdit, QSpinBox, QFileDialog,
    QComboBox, QDoubleSpinBox,
    QGroupBox, QFrame, QSizePolicy, QScrollArea, QMessageBox
)
from PyQt5.QtCore import Qt, pyqtSignal, QTimer, QEvent, QObject
from PyQt5.QtGui import QFont, QColor, QTextCursor, QIcon

from .models import AppState
from .logger import log, Logger
from config import Config


# 状态颜色映射
STATE_COLORS = {
    AppState.IDLE: "#888888",
    AppState.CAPTURING: "#2196F3",
    AppState.VL_RECOGNIZING: "#9C27B0",
    AppState.WAITING_AGENT: "#FF9800",
    AppState.TYPING: "#4CAF50",
    AppState.HISTORY_COLLECTING: "#00BCD4",
    AppState.PAUSED: "#FF5722",
    AppState.EMERGENCY_STOPPED: "#F44336",
}


class WheelGuard(QObject):
    """滚轮守卫：未聚焦的 SpinBox/ComboBox 忽略滚轮事件，
    让滚轮用于滚动容器而不是误改数值。"""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Wheel and not obj.hasFocus():
            return True
        return super().eventFilter(obj, event)


class CollapsibleSection(QWidget):
    """可折叠分组：点击标题栏展开/收起内容。"""

    def __init__(self, title: str, expanded: bool = True, parent=None):
        super().__init__(parent)
        self._expanded = expanded

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        self._header = QPushButton()
        self._header.setCheckable(True)
        self._header.setChecked(expanded)
        self._header.setCursor(Qt.PointingHandCursor)
        self._header.setStyleSheet(
            "QPushButton{text-align:left;background:#eef2ff;color:#1e3a8a;"
            "border:1px solid #e0e7ff;border-radius:6px;padding:6px 10px;"
            "font-weight:600;}"
            "QPushButton:hover{background:#e0e7ff;}"
        )
        self._header.clicked.connect(self._toggle)
        outer.addWidget(self._header)

        self._content = QWidget()
        self._form = QFormLayout(self._content)
        self._form.setContentsMargins(4, 6, 4, 4)
        self._form.setVerticalSpacing(8)
        outer.addWidget(self._content)

        self.set_title(title)
        self._content.setVisible(expanded)

    def set_title(self, title: str):
        arrow = "▼" if self._expanded else "▶"
        self._header.setText(f" {arrow}  {title}")

    def _toggle(self):
        self._expanded = not self._expanded
        self._content.setVisible(self._expanded)
        self.set_title(self._header.text().strip().lstrip("▼▶").strip())

    @property
    def form(self) -> QFormLayout:
        return self._form


class ControlPanel(QWidget):
    """主控制面板窗口"""

    # --- 对外信号 ---
    start_pause_requested = pyqtSignal()
    history_collect_requested = pyqtSignal()
    emergency_stop_requested = pyqtSignal()
    selection_mode_requested = pyqtSignal()
    config_saved = pyqtSignal(object)  # Config
    test_api_requested = pyqtSignal()  # 测试 API 连通性
    cu_goal_changed = pyqtSignal(str)  # CU 提示词实时变更

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self._config = config
        self._is_running = False  # 自动化是否运行中
        self._init_ui()
        self._load_config_to_ui()
        self._connect_log_signal()
        self._connect_shortcuts()

        self.setWindowTitle("聊天辅助工具 - 控制面板")
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.resize(560, 760)
        self.setMinimumSize(460, 540)

    # ================================================================
    #  UI 构建
    # ================================================================
    def _init_ui(self):
        # ---- 全局统一样式（配色 / 圆角 / 边框 / 字体）----
        self.setStyleSheet("""
            QWidget {
                background-color: #f3f5f9;
                color: #1f2937;
                font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
                font-size: 9.5pt;
            }
            QGroupBox {
                background-color: #ffffff;
                border: 1px solid #e4e7ec;
                border-radius: 8px;
                margin-top: 14px;
                padding: 12px 10px 10px 10px;
                font-weight: 600;
                color: #374151;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 12px;
                padding: 0 6px;
                color: #2563eb;
                background-color: #f3f5f9;
            }
            QLabel { background-color: transparent; }
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
                background-color: #ffffff;
                border: 1px solid #d1d5db;
                border-radius: 6px;
                padding: 6px 8px;
                selection-background-color: #2563eb;
            }
            QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
                border: 1px solid #2563eb;
            }
            QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {
                border: 1px solid #9ca3af;
            }
            QComboBox::drop-down {
                border: none;
                width: 22px;
            }
            QComboBox QAbstractItemView {
                border: 1px solid #d1d5db;
                border-radius: 6px;
                background-color: #ffffff;
                selection-background-color: #e0e7ff;
                selection-color: #1f2937;
                outline: none;
            }
            QPushButton {
                border: none;
                border-radius: 6px;
                padding: 7px 14px;
                color: #ffffff;
                font-weight: 600;
            }
            QPushButton:hover { padding: 7px 14px; }
            QScrollArea { border: none; background-color: transparent; }
            QScrollArea > QWidget > QWidget { background-color: transparent; }
            QScrollBar:vertical {
                background: transparent; width: 10px; margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #cbd5e1; border-radius: 5px; min-height: 30px;
            }
            QScrollBar::handle:vertical:hover { background: #94a3b8; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar:horizontal {
                background: transparent; height: 10px; margin: 0;
            }
            QScrollBar::handle:horizontal {
                background: #cbd5e1; border-radius: 5px; min-width: 30px;
            }
            QScrollBar::handle:horizontal:hover { background: #94a3b8; }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
        """)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(14, 12, 14, 12)
        main_layout.setSpacing(12)

        # --- 标题栏 ---
        title_bar = QFrame()
        title_bar.setStyleSheet(
            "QFrame{background-color:#2563eb;border-radius:10px;}"
        )
        title_layout = QVBoxLayout(title_bar)
        title_layout.setContentsMargins(16, 12, 16, 12)
        title_layout.setSpacing(2)

        title = QLabel("聊天辅助工具")
        title.setFont(QFont("Microsoft YaHei", 15, QFont.Bold))
        title.setStyleSheet("color:#ffffff;background:transparent;")
        title.setAlignment(Qt.AlignCenter)
        title_layout.addWidget(title)

        subtitle = QLabel("Overlay 选区 · VL 视觉识别 · AI Agent · Computer Use")
        subtitle.setFont(QFont("Microsoft YaHei", 8))
        subtitle.setStyleSheet("color:#c7d2fe;background:transparent;")
        subtitle.setAlignment(Qt.AlignCenter)
        title_layout.addWidget(subtitle)

        main_layout.addWidget(title_bar)

        risk_label = QLabel(
            "⚠ 风险提示：本工具为技术研究原型，自动操作聊天软件存在账号风控风险，"
            "所有风险由使用者自行承担。"
        )
        risk_label.setStyleSheet(
            "color:#b91c1c;background:#fee2e2;border:1px solid #fecaca;"
            "padding:6px 10px;border-radius:6px;"
        )
        risk_label.setFont(QFont("Microsoft YaHei", 8))
        risk_label.setWordWrap(True)
        main_layout.addWidget(risk_label)

        # --- 状态区 ---
        state_group = QGroupBox("运行状态")
        state_layout = QGridLayout()
        state_layout.setHorizontalSpacing(10)
        state_layout.setVerticalSpacing(8)

        self._state_label = QLabel("休眠中")
        self._state_label.setFont(QFont("Microsoft YaHei", 11, QFont.Bold))
        self._state_label.setStyleSheet(
            "color:#6b7280;background:#f3f4f6;border:1px solid #e5e7eb;"
            "padding:6px 12px;border-radius:14px;"
        )
        self._state_label.setAlignment(Qt.AlignCenter)
        state_layout.addWidget(QLabel("当前状态:"), 0, 0)
        state_layout.addWidget(self._state_label, 0, 1)

        self._last_msg_label = QLabel("(无)")
        self._last_msg_label.setWordWrap(True)
        self._last_msg_label.setStyleSheet(
            "background:#f9fafb;border:1px solid #e5e7eb;"
            "padding:7px 10px;border-radius:6px;min-height:22px;color:#374151;"
        )
        state_layout.addWidget(QLabel("最近消息:"), 1, 0)
        state_layout.addWidget(self._last_msg_label, 1, 1)

        state_group.setLayout(state_layout)
        main_layout.addWidget(state_group)

        # --- 操作按钮区 ---
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)

        self._btn_start = QPushButton("启动自动化")
        self._btn_start.setStyleSheet(
            "QPushButton{background:#16a34a;}"
            "QPushButton:hover{background:#15803d;}"
        )
        self._btn_start.clicked.connect(self._on_start_pause)

        self._btn_history = QPushButton("历史采集")
        self._btn_history.setStyleSheet(
            "QPushButton{background:#2563eb;}"
            "QPushButton:hover{background:#1d4ed8;}"
        )
        self._btn_history.clicked.connect(self.history_collect_requested.emit)

        self._btn_select = QPushButton("设置选区")
        self._btn_select.setStyleSheet(
            "QPushButton{background:#475569;}"
            "QPushButton:hover{background:#334155;}"
        )
        self._btn_select.clicked.connect(self.selection_mode_requested.emit)

        self._btn_stop = QPushButton("紧急终止")
        self._btn_stop.setStyleSheet(
            "QPushButton{background:#dc2626;}"
            "QPushButton:hover{background:#b91c1c;}"
        )
        self._btn_stop.clicked.connect(self._on_emergency_stop)

        self._btn_test = QPushButton("测试API")
        self._btn_test.setStyleSheet(
            "QPushButton{background:#64748b;}"
            "QPushButton:hover{background:#475569;}"
        )
        self._btn_test.clicked.connect(self.test_api_requested.emit)

        btn_layout.addWidget(self._btn_start)
        btn_layout.addWidget(self._btn_history)
        btn_layout.addWidget(self._btn_select)
        btn_layout.addWidget(self._btn_stop)
        btn_layout.addWidget(self._btn_test)
        main_layout.addLayout(btn_layout)

        # --- 模式选择区 ---
        mode_group = QGroupBox("运行模式")
        mode_layout = QHBoxLayout()

        self._cfg_run_mode = QComboBox()
        self._cfg_run_mode.addItems(["vl_agent", "computer_use"])
        self._cfg_run_mode.currentTextChanged.connect(self._on_mode_changed)
        mode_layout.addWidget(QLabel("模式:"))
        mode_layout.addWidget(self._cfg_run_mode)

        self._mode_desc_label = QLabel("")
        self._mode_desc_label.setWordWrap(True)
        self._mode_desc_label.setStyleSheet(
            "color:#6b7280;font-size:9pt;padding:2px 4px;"
        )
        mode_layout.addWidget(self._mode_desc_label, 1)

        mode_group.setLayout(mode_layout)
        main_layout.addWidget(mode_group)

        # 快捷键提示
        hotkey_label = QLabel(
            f"快捷键: {self._config.hotkey_start_pause}=启动/暂停    "
            f"{self._config.hotkey_history_collect}=历史采集    "
            f"{self._config.hotkey_emergency_stop}=紧急终止"
        )
        hotkey_label.setFont(QFont("Consolas", 8))
        hotkey_label.setStyleSheet(
            "color:#64748b;background:#ffffff;border:1px solid #e4e7ec;"
            "padding:5px 10px;border-radius:6px;"
        )
        hotkey_label.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(hotkey_label)

        # --- 配置区（可折叠分组，减少滚动）---
        config_container = QWidget()
        config_layout = QVBoxLayout(config_container)
        config_layout.setContentsMargins(2, 2, 2, 2)
        config_layout.setSpacing(8)

        self._wheel_guard = WheelGuard()

        # —— 基础配置 ——
        sec_basic = CollapsibleSection("基础配置")
        f = sec_basic.form

        self._cfg_poll_min = QSpinBox()
        self._cfg_poll_min.setRange(1, 3600)
        self._cfg_poll_min.installEventFilter(self._wheel_guard)
        self._cfg_poll_max = QSpinBox()
        self._cfg_poll_max.setRange(1, 3600)
        self._cfg_poll_max.installEventFilter(self._wheel_guard)
        poll_h = QHBoxLayout()
        poll_h.addWidget(self._cfg_poll_min)
        poll_h.addWidget(QLabel(" ~ "))
        poll_h.addWidget(self._cfg_poll_max)
        poll_h.addStretch()
        poll_w = QWidget()
        poll_w.setLayout(poll_h)
        f.addRow("轮询间隔(秒):", poll_w)

        self._cfg_reply_limit = QSpinBox()
        self._cfg_reply_limit.setRange(1, 999)
        self._cfg_reply_limit.installEventFilter(self._wheel_guard)
        f.addRow("每小时回复上限:", self._cfg_reply_limit)
        config_layout.addWidget(sec_basic)

        # —— VL 视觉模型 ——
        sec_vl = CollapsibleSection("VL 视觉模型")
        f = sec_vl.form

        self._cfg_vl_provider = QComboBox()
        self._cfg_vl_provider.addItems(["dashscope", "openrouter", "custom"])
        self._cfg_vl_provider.installEventFilter(self._wheel_guard)
        self._cfg_vl_provider.currentTextChanged.connect(
            lambda t: self._on_provider_changed("vl", t))
        f.addRow("VL Provider:", self._cfg_vl_provider)

        self._cfg_vl_url = QLineEdit()
        self._cfg_vl_url.setPlaceholderText("https://openrouter.ai/api/v1/chat/completions")
        f.addRow("VL模型地址:", self._cfg_vl_url)

        self._cfg_vl_key = QLineEdit()
        self._cfg_vl_key.setEchoMode(QLineEdit.Password)
        self._cfg_vl_key.setPlaceholderText("sk-...")
        f.addRow("VL API Key:", self._cfg_vl_key)

        self._cfg_vl_model = QLineEdit()
        self._cfg_vl_model.setPlaceholderText(
            "多模态模型: 阿里 qwen-vl-max | OpenRouter qwen/qwen-2.5-vl-72b-instruct"
        )
        self._cfg_vl_model.textChanged.connect(self._validate_vl_model)
        f.addRow("VL模型名称:", self._cfg_vl_model)

        self._vl_model_warning = QLabel("")
        self._vl_model_warning.setStyleSheet("color:#dc2626;font-size:8pt;")
        self._vl_model_warning.setWordWrap(True)
        f.addRow("", self._vl_model_warning)
        config_layout.addWidget(sec_vl)

        # —— AI Agent 模型 ——
        sec_agent = CollapsibleSection("AI Agent 模型")
        f = sec_agent.form

        self._cfg_agent_provider = QComboBox()
        self._cfg_agent_provider.addItems(["dashscope", "openrouter", "custom"])
        self._cfg_agent_provider.installEventFilter(self._wheel_guard)
        self._cfg_agent_provider.currentTextChanged.connect(
            lambda t: self._on_provider_changed("agent", t))
        f.addRow("Agent Provider:", self._cfg_agent_provider)

        self._cfg_agent_url = QLineEdit()
        self._cfg_agent_url.setPlaceholderText("https://openrouter.ai/api/v1/chat/completions")
        f.addRow("AI Agent地址:", self._cfg_agent_url)

        self._cfg_agent_key = QLineEdit()
        self._cfg_agent_key.setEchoMode(QLineEdit.Password)
        self._cfg_agent_key.setPlaceholderText("sk-...")
        f.addRow("Agent API Key:", self._cfg_agent_key)

        self._cfg_agent_model = QLineEdit()
        self._cfg_agent_model.setPlaceholderText(
            "文本模型: 阿里 qwen-plus | OpenRouter deepseek/deepseek-chat"
        )
        f.addRow("Agent模型名称:", self._cfg_agent_model)
        config_layout.addWidget(sec_agent)

        # —— Computer Use 参数 ——
        sec_cu = CollapsibleSection("Computer Use 参数")
        f = sec_cu.form

        # 鼠标移动模式: human=贝塞尔拟人轨迹(慢), fast=瞬时直达(快)
        self._cfg_mouse_mode = QComboBox()
        self._cfg_mouse_mode.addItems(["human", "fast"])
        self._cfg_mouse_mode.installEventFilter(self._wheel_guard)
        f.addRow("鼠标移动模式:", self._cfg_mouse_mode)

        self._cfg_cu_max_steps = QSpinBox()
        self._cfg_cu_max_steps.setRange(1, 50)
        self._cfg_cu_max_steps.installEventFilter(self._wheel_guard)
        f.addRow("CU最大步数:", self._cfg_cu_max_steps)

        self._cfg_cu_step_delay_min = QDoubleSpinBox()
        self._cfg_cu_step_delay_min.setRange(0.0, 30.0)
        self._cfg_cu_step_delay_min.setSingleStep(0.1)
        self._cfg_cu_step_delay_min.setSuffix(" s")
        self._cfg_cu_step_delay_min.installEventFilter(self._wheel_guard)
        self._cfg_cu_step_delay_max = QDoubleSpinBox()
        self._cfg_cu_step_delay_max.setRange(0.0, 30.0)
        self._cfg_cu_step_delay_max.setSingleStep(0.1)
        self._cfg_cu_step_delay_max.setSuffix(" s")
        self._cfg_cu_step_delay_max.installEventFilter(self._wheel_guard)
        cu_delay_h = QHBoxLayout()
        cu_delay_h.addWidget(self._cfg_cu_step_delay_min)
        cu_delay_h.addWidget(QLabel(" ~ "))
        cu_delay_h.addWidget(self._cfg_cu_step_delay_max)
        cu_delay_w = QWidget()
        cu_delay_h.addStretch()
        cu_delay_w.setLayout(cu_delay_h)
        f.addRow("步前停顿(秒):", cu_delay_w)

        self._cfg_cu_screenshot_wait = QDoubleSpinBox()
        self._cfg_cu_screenshot_wait.setRange(0.1, 10.0)
        self._cfg_cu_screenshot_wait.setSingleStep(0.1)
        self._cfg_cu_screenshot_wait.setSuffix(" s")
        self._cfg_cu_screenshot_wait.installEventFilter(self._wheel_guard)
        f.addRow("截图等待(秒):", self._cfg_cu_screenshot_wait)

        # 操作提示词（Computer Use 模式的目标指令）
        self._cfg_cu_goal = QTextEdit()
        self._cfg_cu_goal.setMaximumHeight(80)
        self._cfg_cu_goal.setPlaceholderText(
            "Computer Use 模式的操作目标提示词，例如：\n"
            "查看聊天界面，如果对方发来新消息，请生成自然口语化的回复并点击发送；如无新消息则直接完成。"
        )
        self._cfg_cu_goal.setFont(QFont("Microsoft YaHei", 9))
        self._cfg_cu_goal.setStyleSheet(
            "QTextEdit{background:#ffffff;border:1px solid #d1d5db;border-radius:6px;"
            "padding:6px;}"
            "QTextEdit:focus{border:1px solid #2563eb;}"
        )
        # 运行中也可编辑，文本变更时实时发射信号
        self._cfg_cu_goal.textChanged.connect(
            lambda: self.cu_goal_changed.emit(self._cfg_cu_goal.toPlainText().strip())
        )
        f.addRow("CU操作提示词:", self._cfg_cu_goal)
        config_layout.addWidget(sec_cu)

        # —— 存储与保存 ——
        sec_misc = CollapsibleSection("存储与保存")
        f = sec_misc.form

        self._cfg_storage_path = QLineEdit()
        self._cfg_storage_path.setPlaceholderText("数据存储路径")
        browse_btn = QPushButton("浏览")
        browse_btn.setStyleSheet(
            "QPushButton{background:#e2e8f0;color:#334155;}"
            "QPushButton:hover{background:#cbd5e1;}"
        )
        browse_btn.clicked.connect(self._on_browse_storage)
        path_h = QHBoxLayout()
        path_h.addWidget(self._cfg_storage_path)
        path_h.addWidget(browse_btn)
        path_w = QWidget()
        path_w.setLayout(path_h)
        f.addRow("存储路径:", path_w)

        self._btn_save_config = QPushButton("保存配置")
        self._btn_save_config.setStyleSheet(
            "QPushButton{background:#f59e0b;}"
            "QPushButton:hover{background:#d97706;}"
        )
        self._btn_save_config.clicked.connect(self._on_save_config)
        f.addRow("", self._btn_save_config)
        config_layout.addWidget(sec_misc)

        config_layout.addStretch()

        # 用 QScrollArea 包裹配置区，折叠后高度自适应
        config_scroll = QScrollArea()
        config_scroll.setWidget(config_container)
        config_scroll.setWidgetResizable(True)
        config_scroll.setFrameShape(QFrame.NoFrame)
        main_layout.addWidget(config_scroll, 1)

        # --- 日志区 ---
        log_group = QGroupBox("日志输出")
        log_layout = QVBoxLayout()
        log_layout.setSpacing(8)

        self._log_text = QTextEdit()
        self._log_text.setReadOnly(True)
        self._log_text.setFont(QFont("Consolas", 9))
        self._log_text.setStyleSheet(
            "QTextEdit{background:#0f172a;color:#e2e8f0;border:1px solid #1e293b;"
            "border-radius:6px;padding:6px;}"
        )
        self._log_text.setMaximumHeight(200)
        log_layout.addWidget(self._log_text)

        log_btn_h = QHBoxLayout()
        log_btn_h.setSpacing(8)
        self._btn_clear_log = QPushButton("清空日志")
        self._btn_clear_log.setStyleSheet(
            "QPushButton{background:#e2e8f0;color:#334155;}"
            "QPushButton:hover{background:#cbd5e1;}"
        )
        self._btn_clear_log.clicked.connect(self._log_text.clear)
        log_btn_h.addWidget(self._btn_clear_log)
        log_btn_h.addStretch()
        self._btn_export_log = QPushButton("导出日志")
        self._btn_export_log.setStyleSheet(
            "QPushButton{background:#e2e8f0;color:#334155;}"
            "QPushButton:hover{background:#cbd5e1;}"
        )
        self._btn_export_log.clicked.connect(self._on_export_log)
        log_btn_h.addWidget(self._btn_export_log)
        log_layout.addLayout(log_btn_h)

        log_group.setLayout(log_layout)
        main_layout.addWidget(log_group)

        self.setLayout(main_layout)

    # ================================================================
    #  快捷键
    # ================================================================
    def _connect_shortcuts(self):
        from PyQt5.QtWidgets import QShortcut
        from PyQt5.QtGui import QKeySequence

        QShortcut(QKeySequence(self._config.hotkey_start_pause),
                  self, activated=self._on_start_pause)
        QShortcut(QKeySequence(self._config.hotkey_history_collect),
                  self, activated=self.history_collect_requested.emit)
        QShortcut(QKeySequence(self._config.hotkey_emergency_stop),
                  self, activated=self._on_emergency_stop)

    def _on_provider_changed(self, which: str, provider: str):
        """provider 切换时自动填充默认 URL 和模型名（custom 不覆盖已填值）"""
        if provider == "openrouter":
            url = "https://openrouter.ai/api/v1/chat/completions"
            vl_model = "qwen/qwen-2.5-vl-72b-instruct"
            agent_model = "deepseek/deepseek-chat"
        elif provider == "dashscope":
            url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
            vl_model = "qwen-vl-max"
            agent_model = "qwen-plus"
        else:
            # custom：URL 保持用户已填值，仅在空时给模型名默认值
            url = None
            vl_model = "qwen-vl-max"
            agent_model = "qwen-plus"
        if which == "vl":
            if url is not None and (
                not self._cfg_vl_url.text()
                or self._cfg_vl_url.text().startswith("https://openrouter")
                or self._cfg_vl_url.text().startswith("https://dashscope")
            ):
                self._cfg_vl_url.setText(url)
            if not self._cfg_vl_model.text():
                self._cfg_vl_model.setText(vl_model)
        elif which == "agent":
            if url is not None and (
                not self._cfg_agent_url.text()
                or self._cfg_agent_url.text().startswith("https://openrouter")
                or self._cfg_agent_url.text().startswith("https://dashscope")
            ):
                self._cfg_agent_url.setText(url)
            if not self._cfg_agent_model.text():
                self._cfg_agent_model.setText(agent_model)

    def _validate_vl_model(self):
        """校验 VL 模型名是否为多模态模型"""
        name = self._cfg_vl_model.text().strip().lower()
        if not name:
            self._vl_model_warning.setText(
                "⚠ 提示: 必须填多模态(VL)模型，如 阿里 qwen-vl-max 或 qwen2.5-vl-72b-instruct"
            )
            return
        # 多模态模型通常含 vl / vision / multimodal / gpt-4o
        vl_keywords = ("vl", "vision", "multimodal", "gpt-4o", "llava", "internvl")
        if not any(kw in name for kw in vl_keywords):
            self._vl_model_warning.setText(
                "⚠ 警告: 该模型名不含 vl/vision，可能是纯文本模型，无法分析截图！"
            )
        else:
            self._vl_model_warning.setText("")

    def _on_mode_changed(self, mode: str):
        """运行模式切换时更新说明文字"""
        if mode == "computer_use":
            self._mode_desc_label.setText(
                "Computer Use 模式：截图→多模态LLM输出动作→键鼠执行→循环。"
                "通用聊天软件（微信/QQ/钉钉/飞书等），只需 VL 多模态模型。"
            )
        else:
            self._mode_desc_label.setText(
                "VL+Agent 模式：定时快照→VL识别消息→AI Agent生成回复→键鼠发送。"
                "需要 VL 和 Agent 两个模型。"
            )


    # ================================================================
    #  日志连接
    # ================================================================
    def _connect_log_signal(self):
        Logger.get_signal().log_emitted.connect(self._append_log)

    def _append_log(self, level: str, message: str):
        color_map = {
            "DEBUG": "#888",
            "INFO": "#4FC3F7",
            "WARNING": "#FFD54F",
            "ERROR": "#EF5350",
        }
        color = color_map.get(level, "#d4d4d4")
        # 提取时间戳部分
        parts = message.split("] ", 2)
        if len(parts) >= 3:
            timestamp = parts[0].lstrip("[")
            lvl = parts[1].lstrip("[")
            msg = parts[2]
        else:
            timestamp = lvl = ""
            msg = message

        html = (f'<span style="color:#666;">[{timestamp}]</span> '
                f'<span style="color:{color};font-weight:bold;">[{lvl}]</span> '
                f'<span style="color:{color};">{msg}</span><br>')
        self._log_text.append(html)
        self._log_text.moveCursor(QTextCursor.End)

    # ================================================================
    #  配置读写
    # ================================================================
    def _load_config_to_ui(self):
        c = self._config
        self._cfg_run_mode.setCurrentText(c.run_mode)
        self._cfg_poll_min.setValue(c.poll_interval_min)
        self._cfg_poll_max.setValue(c.poll_interval_max)
        self._cfg_reply_limit.setValue(c.reply_limit_per_hour)
        self._cfg_vl_provider.setCurrentText(c.vl_provider)
        self._cfg_vl_url.setText(c.vl_api_url)
        self._cfg_vl_key.setText(c.vl_api_key)
        self._cfg_vl_model.setText(c.vl_model_name)
        self._cfg_agent_provider.setCurrentText(c.agent_provider)
        self._cfg_agent_url.setText(c.agent_api_url)
        self._cfg_agent_key.setText(c.agent_api_key)
        self._cfg_agent_model.setText(c.agent_model_name)
        self._cfg_mouse_mode.setCurrentText(
            getattr(c, "mouse_move_mode", "human") or "human")
        self._cfg_cu_max_steps.setValue(c.cu_max_steps)
        self._cfg_cu_step_delay_min.setValue(c.cu_step_delay_min)
        self._cfg_cu_step_delay_max.setValue(c.cu_step_delay_max)
        self._cfg_cu_screenshot_wait.setValue(c.cu_screenshot_interval)
        self._cfg_cu_goal.setPlainText(c.cu_goal or "")
        self._cfg_storage_path.setText(c.storage_path)
        # 触发模式说明更新
        self._on_mode_changed(c.run_mode)

    def _collect_config_from_ui(self) -> Config:
        c = self._config
        c.run_mode = self._cfg_run_mode.currentText()
        c.poll_interval_min = self._cfg_poll_min.value()
        c.poll_interval_max = self._cfg_poll_max.value()
        c.reply_limit_per_hour = self._cfg_reply_limit.value()
        c.vl_provider = self._cfg_vl_provider.currentText()
        c.vl_api_url = self._cfg_vl_url.text().strip() or c.vl_api_url
        c.vl_api_key = self._cfg_vl_key.text().strip()
        c.vl_model_name = self._cfg_vl_model.text().strip() or "qwen-vl-max"
        c.agent_provider = self._cfg_agent_provider.currentText()
        c.agent_api_url = self._cfg_agent_url.text().strip() or c.agent_api_url
        c.agent_api_key = self._cfg_agent_key.text().strip()
        c.agent_model_name = self._cfg_agent_model.text().strip() or "qwen-plus"
        c.mouse_move_mode = self._cfg_mouse_mode.currentText()
        c.cu_max_steps = self._cfg_cu_max_steps.value()
        c.cu_step_delay_min = self._cfg_cu_step_delay_min.value()
        c.cu_step_delay_max = self._cfg_cu_step_delay_max.value()
        c.cu_screenshot_interval = self._cfg_cu_screenshot_wait.value()
        c.cu_goal = self._cfg_cu_goal.toPlainText().strip()
        c.storage_path = self._cfg_storage_path.text().strip() or c.storage_path
        return c

    def _on_save_config(self):
        c = self._collect_config_from_ui()
        try:
            c.save()
            log.info("配置已保存")
            self.config_saved.emit(c)
            QMessageBox.information(self, "成功", "配置已保存")
        except Exception as e:
            log.error(f"配置保存失败: {e}")
            QMessageBox.critical(self, "错误", f"配置保存失败: {e}")

    def _on_browse_storage(self):
        path = QFileDialog.getExistingDirectory(self, "选择存储路径")
        if path:
            self._cfg_storage_path.setText(path)

    # ================================================================
    #  按钮事件
    # ================================================================
    def _on_start_pause(self):
        self._is_running = not self._is_running
        if self._is_running:
            self._btn_start.setText("暂停自动化")
            self._btn_start.setStyleSheet(
                "QPushButton{background:#f59e0b;}"
                "QPushButton:hover{background:#d97706;}"
            )
        else:
            self._btn_start.setText("启动自动化")
            self._btn_start.setStyleSheet(
                "QPushButton{background:#16a34a;}"
                "QPushButton:hover{background:#15803d;}"
            )
        self.start_pause_requested.emit()

    def _on_emergency_stop(self):
        self._is_running = False
        self._btn_start.setText("启动自动化")
        self._btn_start.setStyleSheet(
            "QPushButton{background:#16a34a;}"
            "QPushButton:hover{background:#15803d;}"
        )
        log.warning("紧急终止已触发！")
        self.emergency_stop_requested.emit()

    def _on_export_log(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出日志", "log_export.txt", "Text Files (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._log_text.toPlainText())
            log.info(f"日志已导出到 {path}")

    # ================================================================
    #  外部推送接口
    # ================================================================
    def update_state(self, state: AppState):
        """更新状态显示（供调度器调用）"""
        color = STATE_COLORS.get(state, "#6b7280")
        self._state_label.setText(state.value)
        self._state_label.setStyleSheet(
            f"color:{color};background:#f3f4f6;border:1px solid {color};"
            f"padding:6px 12px;border-radius:14px;font-weight:bold;"
        )

    def update_last_message(self, sender: str, text: str):
        """更新最近消息显示（供调度器调用）"""
        prefix = "[对方]" if sender == "other" else "[我方]"
        display = f"{prefix} {text}"
        if len(display) > 60:
            display = display[:57] + "..."
        self._last_msg_label.setText(display)

    def reset_start_button(self):
        """外部停止后重置按钮状态"""
        self._is_running = False
        self._btn_start.setText("启动自动化")
        self._btn_start.setStyleSheet(
            "QPushButton{background:#4CAF50;color:white;font-size:13px;"
            "padding:8px 16px;border-radius:4px;font-weight:bold;}"
            "QPushButton:hover{background:#43A047;}"
        )
