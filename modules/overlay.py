"""
Overlay 悬浮选区模块
====================
职责：
1. 框内淡灰色蒙版标记操作区域，框外完全透明且鼠标穿透（不影响外部操作）
2. 红色边框选区 + 四角强化 + 右下角缩放手柄
3. 拖拽选区移动、缩放、方向键微调
4. 支持多显示器（虚拟桌面）
5. 截图时临时移除蒙版，避免遮挡画面

信号：
- rect_changed(Rect):  选区移动或缩放时发射
"""
from PyQt5.QtWidgets import QWidget, QApplication
from PyQt5.QtCore import Qt, pyqtSignal as Signal, QRect, QPoint
from PyQt5.QtGui import QPainter, QPen, QColor, QBrush, QFont, QRegion

from .models import Rect
from .logger import log


class OverlayWindow(QWidget):
    """悬浮选区窗口"""

    rect_changed = Signal(object)  # 发射 Rect
    # 线程安全信号：后台线程通过 emit 请求主线程更新 UI
    _capture_mode_requested = Signal(bool)  # True=进入截图模式, False=退出

    _DRAG_NONE = 0
    _DRAG_MOVE = 1
    _DRAG_RESIZE = 2

    RESIZE_HANDLE_SIZE = 14
    MIN_SELECTION_W = 100
    MIN_SELECTION_H = 100

    def __init__(self, initial_rect: Rect = None):
        super().__init__()

        # --- 窗口属性 ---
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        # --- 虚拟桌面范围（支持多屏）---
        screens = QApplication.screens()
        xs = [s.geometry().x() for s in screens]
        ys = [s.geometry().y() for s in screens]
        xs_end = [s.geometry().x() + s.geometry().width() for s in screens]
        ys_end = [s.geometry().y() + s.geometry().height() for s in screens]
        self._desktop_x = min(xs)
        self._desktop_y = min(ys)
        self._desktop_w = max(xs_end) - self._desktop_x
        self._desktop_h = max(ys_end) - self._desktop_y

        # 窗口覆盖整个虚拟桌面
        self.setGeometry(self._desktop_x, self._desktop_y,
                         self._desktop_w, self._desktop_h)

        # --- 选区 ---
        if initial_rect:
            self._rect = initial_rect
        else:
            w, h = 420, 520
            # 默认放在主屏右中位置
            primary = QApplication.primaryScreen().geometry()
            left = primary.x() + primary.width() - w - 100
            top = primary.y() + (primary.height() - h) // 2
            self._rect = Rect(left, top, w, h)

        # --- 交互状态 ---
        self._drag_mode = self._DRAG_NONE
        self._drag_start_pos: tuple = None
        self._drag_start_rect: Rect = None
        self._locked = False
        self._capture_mode = False

        self.setMouseTracking(True)
        self._update_mask()

        # 信号连接：后台线程通过信号安全地请求主线程更新 UI
        self._capture_mode_requested.connect(self._do_capture_mode)

        log.info(f"Overlay 初始化，虚拟桌面: ({self._desktop_x},{self._desktop_y}) "
                 f"{self._desktop_w}x{self._desktop_h}")
        log.info(f"选区: ({self._rect.left},{self._rect.top}) "
                 f"{self._rect.width}x{self._rect.height}")

    # ================================================================
    #  坐标转换：屏幕坐标 <-> 窗口坐标
    # ================================================================
    def _to_win_x(self, x: int) -> int:
        return x - self._desktop_x

    def _to_win_y(self, y: int) -> int:
        return y - self._desktop_y

    def _selection_qrect(self) -> QRect:
        """选区在窗口坐标系下的 QRect"""
        return QRect(
            self._to_win_x(self._rect.left),
            self._to_win_y(self._rect.top),
            self._rect.width,
            self._rect.height,
        )

    # ================================================================
    #  Mask：选区外鼠标穿透
    # ================================================================
    def _update_mask(self):
        """设置窗口可交互区域 = 选区范围，选区外完全穿透"""
        if self._capture_mode:
            # 截图模式：清除 mask 让窗口完全透明穿透
            self.clearMask()
        else:
            sel = self._selection_qrect()
            # 稍微扩大以包含边框线
            region = QRegion(sel.adjusted(-2, -2, 3, 3))
            self.setMask(region)

    # ================================================================
    #  绘制
    # ================================================================
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        sel = self._selection_qrect()

        # --- 选区内淡灰色蒙版（截图模式跳过）---
        if not self._capture_mode:
            alpha = 40 if not self._locked else 20
            painter.fillRect(sel, QColor(80, 80, 80, alpha))

        # --- 红色边框 ---
        pen = QPen(QColor(255, 40, 40), 2)
        painter.setPen(pen)
        painter.drawRect(sel)

        # 四角强化
        cl = 16
        pen2 = QPen(QColor(255, 40, 40), 3)
        painter.setPen(pen2)
        l, t = sel.left(), sel.top()
        r, b = sel.right(), sel.bottom()
        painter.drawLine(l, t, l + cl, t)
        painter.drawLine(l, t, l, t + cl)
        painter.drawLine(r, t, r - cl, t)
        painter.drawLine(r, t, r, t + cl)
        painter.drawLine(l, b, l + cl, b)
        painter.drawLine(l, b, l, b - cl)
        painter.drawLine(r, b, r - cl, b)
        painter.drawLine(r, b, r, b - cl)

        # --- 缩放手柄（未锁定且非截图模式）---
        if not self._locked and not self._capture_mode:
            s = self.RESIZE_HANDLE_SIZE
            handle = QRect(r - s, b - s, s, s)
            painter.setBrush(QBrush(QColor(255, 40, 40)))
            painter.setPen(Qt.NoPen)
            painter.drawRect(handle)

        # --- 坐标信息（选区内部左上角）---
        if not self._capture_mode:
            painter.setPen(QPen(QColor(255, 255, 255)))
            painter.setFont(QFont("Consolas", 9))
            info = f"({self._rect.left}, {self._rect.top})  {self._rect.width}x{self._rect.height}"
            fm = painter.fontMetrics()
            tw = fm.horizontalAdvance(info) + 8
            th = fm.height() + 4
            painter.fillRect(l + 4, t + 4, tw, th, QColor(0, 0, 0, 180))
            painter.drawText(l + 8, t + 4 + fm.ascent(), info)

            if self._locked:
                painter.setPen(QPen(QColor(255, 200, 0)))
                painter.setFont(QFont("Consolas", 8))
                painter.drawText(l + 8, t + th + 14, "[ LOCKED ]")

    # ================================================================
    #  鼠标交互
    # ================================================================
    def _get_resize_handle_rect(self) -> QRect:
        s = self.RESIZE_HANDLE_SIZE
        sel = self._selection_qrect()
        return QRect(sel.right() - s, sel.bottom() - s, s, s)

    def mousePressEvent(self, event):
        if self._locked:
            return
        if event.button() != Qt.LeftButton:
            return

        # event.pos() 是窗口坐标，选区也已转为窗口坐标
        pos = event.pos()
        if self._get_resize_handle_rect().contains(pos):
            self._drag_mode = self._DRAG_RESIZE
        elif self._selection_qrect().contains(pos):
            self._drag_mode = self._DRAG_MOVE

        if self._drag_mode != self._DRAG_NONE:
            self._drag_start_pos = (pos.x(), pos.y())
            self._drag_start_rect = Rect(
                self._rect.left, self._rect.top,
                self._rect.width, self._rect.height,
            )

    def mouseMoveEvent(self, event):
        pos = event.pos()
        # 窗口坐标转回屏幕坐标用于比较
        x = pos.x() + self._desktop_x
        y = pos.y() + self._desktop_y

        if self._drag_mode == self._DRAG_NONE:
            if not self._locked and self._get_resize_handle_rect().contains(pos):
                self.setCursor(Qt.SizeFDiagCursor)
            elif not self._locked and self._selection_qrect().contains(pos):
                self.setCursor(Qt.SizeAllCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
            return

        dx = x - (self._drag_start_pos[0] + self._desktop_x)
        dy = y - (self._drag_start_pos[1] + self._desktop_y)

        if self._drag_mode == self._DRAG_MOVE:
            new_left = self._drag_start_rect.left + dx
            new_top = self._drag_start_rect.top + dy
            # 约束到虚拟桌面范围
            min_x = self._desktop_x
            min_y = self._desktop_y
            max_x = self._desktop_x + self._desktop_w - self._drag_start_rect.width
            max_y = self._desktop_y + self._desktop_h - self._drag_start_rect.height
            new_left = max(min_x, min(new_left, max_x))
            new_top = max(min_y, min(new_top, max_y))
            self._rect.left = new_left
            self._rect.top = new_top

        elif self._drag_mode == self._DRAG_RESIZE:
            new_w = max(self.MIN_SELECTION_W,
                        self._drag_start_rect.width + dx)
            new_h = max(self.MIN_SELECTION_H,
                        self._drag_start_rect.height + dy)
            max_w = self._desktop_x + self._desktop_w - self._rect.left
            max_h = self._desktop_y + self._desktop_h - self._rect.top
            self._rect.width = min(new_w, max_w)
            self._rect.height = min(new_h, max_h)

        self.update()
        self._update_mask()
        self.rect_changed.emit(self.get_current_rect())

    def mouseReleaseEvent(self, event):
        if self._drag_mode != self._DRAG_NONE:
            log.debug(f"选区变更完成: ({self._rect.left},{self._rect.top}) "
                      f"{self._rect.width}x{self._rect.height}")
        self._drag_mode = self._DRAG_NONE

    def keyPressEvent(self, event):
        if self._locked:
            return
        step = 10 if event.modifiers() & Qt.ShiftModifier else 1
        moved = True
        max_x = self._desktop_x + self._desktop_w - self._rect.width
        max_y = self._desktop_y + self._desktop_h - self._rect.height
        if event.key() == Qt.Key_Left:
            self._rect.left = max(self._desktop_x, self._rect.left - step)
        elif event.key() == Qt.Key_Right:
            self._rect.left = min(max_x, self._rect.left + step)
        elif event.key() == Qt.Key_Up:
            self._rect.top = max(self._desktop_y, self._rect.top - step)
        elif event.key() == Qt.Key_Down:
            self._rect.top = min(max_y, self._rect.top + step)
        else:
            moved = False
        if moved:
            self.update()
            self._update_mask()
            self.rect_changed.emit(self.get_current_rect())

    # ================================================================
    #  外部接口
    # ================================================================
    def get_current_rect(self) -> Rect:
        return Rect(
            self._rect.left, self._rect.top,
            self._rect.width, self._rect.height,
        )

    def set_rect(self, rect: Rect):
        self._rect = rect
        self.update()
        self._update_mask()
        self.rect_changed.emit(self.get_current_rect())

    def set_locked(self, locked: bool):
        self._locked = locked
        self.update()
        self._update_mask()
        log.info(f"选区{'锁定' if locked else '解锁'}")

    def enter_capture_mode(self):
        """截图前：清除蒙版和mask，避免遮挡画面

        线程安全：通过信号发射到主线程执行，可从任意线程调用。
        """
        self._capture_mode_requested.emit(True)

    def exit_capture_mode(self):
        """截图后：恢复蒙版和mask

        线程安全：通过信号发射到主线程执行，可从任意线程调用。
        """
        self._capture_mode_requested.emit(False)

    def _do_capture_mode(self, enter: bool):
        """实际在主线程中执行蒙版切换（由信号驱动）"""
        self._capture_mode = enter
        if enter:
            self.clearMask()
        else:
            self._update_mask()
        self.update()
