from __future__ import annotations

import html
import json
import math
import os
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import keyring
import requests
from keyring.errors import KeyringError, PasswordDeleteError
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QFont, QIntValidator, QPainter, QPixmap, QTextDocument, QTextOption, QTransform
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "MoeDesktopPet"
KEYRING_SERVICE = "MoeDesktopPet.DeepSeek"
KEYRING_ACCOUNT = "api_key"


def resource_path(*parts: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)


def user_data_dir() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    path = root / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


ASSET_DIR = resource_path("assets")
CONFIG_PATH = user_data_dir() / "config.json"

# 直接在这里修改“多久没有操作后睡觉”的随机范围，单位为秒。
# 例如 (600, 1200) 表示每次活动后随机等待 10～20 分钟再睡觉。
SLEEP_AFTER_RANGE_SECONDS = (600, 1200)

# AI 回复逐字显示速度，单位为毫秒。数值越小，文字出现越快。
TYPEWRITER_INTERVAL_MS = 45

# 桌宠图片上下方的透明交互空间。
TOP_REPLY_SPACE_RATIO = 0.72
BOTTOM_INPUT_SPACE_RATIO = 0.42

REQUIRED_ASSETS = [
    "idle.png",
    "walk1.png",
    "walk2.png",
    "walk3.png",
    "walk4.png",
    "sleep1.png",
    "sleep2.png",
    "speak1.png",
    "speak2.png",
    "drag.png",
]


@dataclass
class AppConfig:
    api_key: str = ""
    api_base: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    system_prompt: str = "你是一只住在用户桌面上的可爱桌宠。回答自然、简洁、有一点个性，但不要过度卖萌。"
    pet_size: int = 260
    walk_speed: int = 3
    auto_walk: bool = True
    always_on_top: bool = True
    auto_talk: bool = False
    auto_talk_min_seconds: int = 300
    auto_talk_max_seconds: int = 900
    reply_display_seconds: int = 12

    @classmethod
    def load(cls) -> "AppConfig":
        config = cls()
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                values = {
                    key: raw[key]
                    for key in cls.__annotations__
                    if key != "api_key" and key in raw
                }
                config = cls(**values)
            except (OSError, ValueError, TypeError):
                config = cls()
        try:
            config.api_key = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT) or ""
        except KeyringError:
            config.api_key = ""
        return config

    def save(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        public_values = {
            key: value for key, value in self.__dict__.items() if key != "api_key"
        }
        CONFIG_PATH.write_text(
            json.dumps(public_values, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_api_key(self) -> None:
        api_key = self.api_key.strip()
        try:
            if api_key:
                keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, api_key)
            else:
                try:
                    keyring.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
                except PasswordDeleteError:
                    pass
        except KeyringError as exc:
            raise RuntimeError(f"无法写入系统凭据存储：{exc}") from exc


class DeepSeekWorker(QObject):
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, config: AppConfig, messages: list[dict]):
        super().__init__()
        self.config = config
        self.messages = messages

    def run(self) -> None:
        if not self.config.api_key.strip():
            self.failed.emit("尚未配置 DeepSeek API Key。请右键桌宠 → 设置。")
            return

        url = self.config.api_base.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.config.model,
            "messages": self.messages,
            "stream": False,
        }
        if self.config.model in {"deepseek-v4-flash", "deepseek-v4-pro"}:
            payload["thinking"] = {"type": "disabled"}

        try:
            response = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.config.api_key.strip()}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=90,
            )
            response.raise_for_status()
            data = response.json()
            text = data["choices"][0]["message"]["content"].strip()
            self.finished.emit(text or "……")
        except requests.HTTPError:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            self.failed.emit(f"DeepSeek API 请求失败：{response.status_code}\n{detail}")
        except Exception as exc:
            self.failed.emit(f"网络请求失败：{exc}")


class EnterTextEdit(QPlainTextEdit):
    submitted = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("和我说点什么……  Enter 发送")
        self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.document().contentsChanged.connect(self.adjust_height)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet(
            "QPlainTextEdit {"
            "background: rgba(255, 255, 255, 238);"
            "border: 1px solid rgba(120, 120, 120, 130);"
            "border-radius: 12px;"
            "padding: 8px 10px;"
            "font-size: 14px;"
            "color: #222222;"
            "selection-background-color: #A4EFEC;"
            "}"
        )
        self.adjust_height()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() & Qt.ShiftModifier:
                super().keyPressEvent(event)
                return
            text = self.toPlainText().strip()
            if text:
                self.submitted.emit(text)
                self.clear()
            return
        super().keyPressEvent(event)

    def adjust_height(self) -> None:
        doc_height = self.document().size().height()
        target = max(42, min(150, int(doc_height + 20)))
        self.setFixedHeight(target)
        parent = self.parentWidget()
        if parent and hasattr(parent, "layout_overlay_widgets"):
            QTimer.singleShot(0, parent.layout_overlay_widgets)


class ReplyBubble(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setTextFormat(Qt.RichText)
        self.setStyleSheet(
            "QLabel {"
            "background: rgba(255, 255, 255, 245);"
            "border: 1px solid rgba(120, 120, 120, 120);"
            "border-radius: 14px;"
            "padding: 10px 12px;"
            "font-size: 14px;"
            "color: #222222;"
            "}"
        )
        self.hide()


class SettingsDialog(QDialog):
    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("萌萌桌宠设置")
        self.setMinimumWidth(500)
        self.config = config

        self.api_key = QLineEdit(config.api_key)
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_base = QLineEdit(config.api_base)

        self.model = QComboBox()
        self.model.addItems(["deepseek-v4-flash", "deepseek-v4-pro"])
        self.model.setCurrentText(config.model)

        self.system_prompt = QPlainTextEdit(config.system_prompt)
        self.system_prompt.setMinimumHeight(100)

        self.pet_size = self.make_integer_input(config.pet_size, 100, 700, "100～700")
        self.walk_speed = self.make_integer_input(config.walk_speed, 1, 15, "1～15")

        self.auto_walk = QCheckBox("允许桌宠自动散步")
        self.auto_walk.setChecked(config.auto_walk)

        self.auto_talk = QCheckBox("允许桌宠隔一段时间主动说话")
        self.auto_talk.setChecked(config.auto_talk)

        self.auto_talk_min = self.make_integer_input(
            config.auto_talk_min_seconds, 30, 86400, "30～86400 秒"
        )
        self.auto_talk_max = self.make_integer_input(
            config.auto_talk_max_seconds, 30, 86400, "30～86400 秒"
        )
        self.reply_display_seconds = self.make_integer_input(
            config.reply_display_seconds, 1, 300, "1～300 秒"
        )

        form = QFormLayout()
        form.addRow("API Key", self.api_key)
        form.addRow("API 地址", self.api_base)
        form.addRow("模型", self.model)
        form.addRow("角色设定", self.system_prompt)
        form.addRow("桌宠显示尺寸（px）", self.pet_size)
        form.addRow("走路速度", self.walk_speed)
        form.addRow("", self.auto_walk)
        form.addRow("", self.auto_talk)
        form.addRow("主动说话最短间隔（秒）", self.auto_talk_min)
        form.addRow("主动说话最长间隔（秒）", self.auto_talk_max)
        form.addRow("AI 回复气泡显示时间（秒）", self.reply_display_seconds)

        save_btn = QPushButton("保存")
        cancel_btn = QPushButton("取消")
        save_btn.clicked.connect(self.validate_and_accept)
        cancel_btn.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(cancel_btn)
        buttons.addWidget(save_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(buttons)

    @staticmethod
    def make_integer_input(value: int, minimum: int, maximum: int, hint: str) -> QLineEdit:
        field = QLineEdit(str(value))
        field.setValidator(QIntValidator(minimum, maximum, field))
        field.setPlaceholderText(hint)
        field.setClearButtonEnabled(True)
        field.setMinimumWidth(150)
        return field

    @staticmethod
    def read_integer(field: QLineEdit, label: str, minimum: int, maximum: int) -> int:
        text = field.text().strip()
        if not text:
            raise ValueError(f"“{label}”不能为空。")
        try:
            value = int(text)
        except ValueError as exc:
            raise ValueError(f"“{label}”必须是整数。") from exc
        if not minimum <= value <= maximum:
            raise ValueError(f"“{label}”必须在 {minimum}～{maximum} 之间。")
        return value

    def validate_and_accept(self) -> None:
        try:
            pet_size = self.read_integer(self.pet_size, "桌宠显示尺寸", 100, 700)
            walk_speed = self.read_integer(self.walk_speed, "走路速度", 1, 15)
            minimum = self.read_integer(self.auto_talk_min, "主动说话最短间隔", 10, 86400)
            maximum = self.read_integer(self.auto_talk_max, "主动说话最长间隔", 10, 86400)
            reply_seconds = self.read_integer(self.reply_display_seconds, "AI 回复气泡显示时间", 1, 300)
            if minimum > maximum:
                raise ValueError("“主动说话最短间隔”不能大于“主动说话最长间隔”。")
        except ValueError as exc:
            QMessageBox.warning(self, "设置值不正确", str(exc))
            return

        self._validated_values = {
            "pet_size": pet_size,
            "walk_speed": walk_speed,
            "auto_talk_min": minimum,
            "auto_talk_max": maximum,
            "reply_seconds": reply_seconds,
        }
        self.accept()

    def apply(self) -> None:
        self.config.api_key = self.api_key.text().strip()
        self.config.api_base = self.api_base.text().strip() or "https://api.deepseek.com"
        self.config.model = self.model.currentText()
        self.config.system_prompt = self.system_prompt.toPlainText().strip()
        values = getattr(self, "_validated_values", None)
        if values is None:
            # 正常情况下保存按钮会先执行 validate_and_accept。这里保留防御性校验。
            self.validate_and_accept()
            values = getattr(self, "_validated_values", None)
            if values is None:
                return
        self.config.pet_size = values["pet_size"]
        self.config.walk_speed = values["walk_speed"]
        self.config.auto_walk = self.auto_walk.isChecked()
        self.config.auto_talk = self.auto_talk.isChecked()
        self.config.auto_talk_min_seconds = values["auto_talk_min"]
        self.config.auto_talk_max_seconds = values["auto_talk_max"]
        self.config.reply_display_seconds = values["reply_seconds"]
        self.config.save()
        self.config.save_api_key()


class PetWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.config = AppConfig.load()
        self.pixmaps: dict[str, QPixmap] = {}
        self.current_frame = "idle"
        self.facing_right = False
        self.drag_offset: Optional[QPoint] = None
        self.is_dragging = False
        self.is_speaking = False
        self.is_thinking = False
        self.is_sleeping = False
        self.sleep_transition = False
        self.walking = False
        self.walk_direction = -1
        self.walk_frame_index = 0
        self.last_activity_ms = 0
        self.sleep_after_ms = 0
        self.zzz_particles: list[dict] = []
        self.history: list[dict] = []
        self.thread: Optional[QThread] = None
        self.worker: Optional[DeepSeekWorker] = None
        self.request_kind = "user"
        self.input_visible_by_hover = False
        self.auto_talk_remaining_ms = 0
        self.reply_hide_timer = QTimer(self)
        self.reply_hide_timer.setSingleShot(True)
        self.reply_hide_timer.timeout.connect(self.hide_reply)

        self.typewriter_timer = QTimer(self)
        self.typewriter_timer.timeout.connect(self.reveal_next_character)
        self.typewriter_full_text = ""
        self.typewriter_visible_text = ""
        self.typewriter_index = 0

        self.load_assets()
        self.apply_window_flags()
        self.setMouseTracking(True)

        self.reply_bubble = ReplyBubble(self)
        self.input_box = EnterTextEdit(self)
        self.input_box.submitted.connect(self.send_user_message)
        self.input_box.hide()
        self.input_box.installEventFilter(self)

        self.resize_for_pet_size(keep_bottom_right=False)
        self.reset_position()

        self.animation_timer = QTimer(self)
        self.animation_timer.timeout.connect(self.update_animation)
        self.animation_timer.start(170)

        self.movement_timer = QTimer(self)
        self.movement_timer.timeout.connect(self.update_movement)
        self.movement_timer.start(30)

        self.behavior_timer = QTimer(self)
        self.behavior_timer.timeout.connect(self.update_behavior)
        self.behavior_timer.start(1000)

        self.zzz_timer = QTimer(self)
        self.zzz_timer.timeout.connect(self.update_zzz)
        self.zzz_timer.start(80)

        self.hover_timer = QTimer(self)
        self.hover_timer.timeout.connect(self.refresh_hover_state)
        self.hover_timer.start(120)

        self.mark_activity()
        self.reset_auto_talk_countdown()

    @property
    def top_space(self) -> int:
        return int(self.config.pet_size * TOP_REPLY_SPACE_RATIO)

    @property
    def bottom_space(self) -> int:
        return int(self.config.pet_size * BOTTOM_INPUT_SPACE_RATIO)

    @property
    def pet_rect(self) -> QRect:
        return QRect(0, self.top_space, self.config.pet_size, self.config.pet_size)

    def load_assets(self) -> None:
        missing = []
        for filename in REQUIRED_ASSETS:
            path = ASSET_DIR / filename
            pixmap = QPixmap(str(path))
            key = path.stem
            if pixmap.isNull():
                missing.append(filename)
            else:
                self.pixmaps[key] = pixmap

        if missing:
            QMessageBox.warning(
                self,
                "缺少素材",
                "以下图片未找到或无法读取：\n"
                + "\n".join(missing)
                + "\n\n请将透明 PNG 放入 assets 文件夹。",
            )

    def apply_window_flags(self) -> None:
        flags = Qt.FramelessWindowHint | Qt.Tool
        if self.config.always_on_top:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)

    def resize_for_pet_size(self, keep_bottom_right=True) -> None:
        old_bottom_right = self.frameGeometry().bottomRight()
        total_height = self.top_space + self.config.pet_size + self.bottom_space
        self.resize(self.config.pet_size, total_height)
        self.input_box.setFixedWidth(max(140, int(self.config.pet_size * 0.9)))
        self.layout_overlay_widgets()
        if keep_bottom_right:
            self.move(old_bottom_right - QPoint(self.width() - 1, self.height() - 1))

    def layout_overlay_widgets(self) -> None:
        input_x = (self.width() - self.input_box.width()) // 2
        input_y = self.pet_rect.bottom() + 8
        self.input_box.move(input_x, input_y)

        if self.reply_bubble.isVisible():
            width = max(150, int(self.config.pet_size * 0.94))
            self.reply_bubble.setFixedWidth(width)
            doc = QTextDocument()
            doc.setDefaultFont(self.reply_bubble.font())
            doc.setTextWidth(width - 26)
            doc.setHtml(self.reply_bubble.text())
            height = max(48, min(self.top_space - 10, int(doc.size().height() + 24)))
            self.reply_bubble.setFixedHeight(height)
            x = (self.width() - width) // 2
            y = self.pet_rect.top() - height - 8
            self.reply_bubble.move(x, max(2, y))

    def mark_activity(self) -> None:
        self.last_activity_ms = 0
        low, high = sorted(SLEEP_AFTER_RANGE_SECONDS)
        self.sleep_after_ms = random.randint(max(1, low), max(1, high)) * 1000
        if self.is_sleeping or self.sleep_transition:
            self.wake_up()

    def reset_auto_talk_countdown(self) -> None:
        low = max(30, self.config.auto_talk_min_seconds)
        high = max(low, self.config.auto_talk_max_seconds)
        self.auto_talk_remaining_ms = random.randint(low, high) * 1000

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        pixmap = self.pixmaps.get(self.current_frame) or self.pixmaps.get("idle")
        if pixmap:
            shown = pixmap
            if self.facing_right:
                shown = pixmap.transformed(QTransform().scale(-1, 1))
            painter.drawPixmap(self.pet_rect, shown)

        if self.is_sleeping:
            painter.setFont(QFont("Segoe UI", max(16, self.width() // 13), QFont.Bold))
            painter.setPen(QColor("#A4EFEC"))
            for particle in self.zzz_particles:
                painter.setOpacity(max(0.0, min(1.0, particle["opacity"])))
                painter.drawText(QPointF(particle["x"], particle["y"]), "Z")
            painter.setOpacity(1.0)

    def eventFilter(self, watched, event):
        if watched is self.input_box:
            if event.type() in (QEvent.FocusIn, QEvent.MouseButtonPress, QEvent.KeyPress):
                self.mark_activity()
            elif event.type() == QEvent.FocusOut:
                QTimer.singleShot(150, self.refresh_hover_state)
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event) -> None:
        self.mark_activity()
        local = event.position().toPoint()
        upper_half = QRect(
            self.pet_rect.left(),
            self.pet_rect.top(),
            self.pet_rect.width(),
            self.pet_rect.height() // 2,
        )
        if event.button() == Qt.LeftButton and upper_half.contains(local):
            self.drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self.is_dragging = True
            self.walking = False
            self.current_frame = "drag"
            self.update()
            event.accept()
        elif event.button() == Qt.RightButton and self.pet_rect.contains(local):
            self.show_context_menu(event.globalPosition().toPoint())

    def mouseMoveEvent(self, event) -> None:
        if self.is_dragging and self.drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if self.is_dragging:
            self.is_dragging = False
            self.drag_offset = None
            if self.is_thinking:
                self.current_frame = "sleep1"
            elif self.is_speaking:
                self.current_frame = "speak1"
            else:
                self.current_frame = "idle"
            self.update()

    def show_context_menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self)

        focus_action = QAction("输入消息", self)
        focus_action.triggered.connect(self.focus_input)
        menu.addAction(focus_action)

        clear_action = QAction("清空对话记录", self)
        clear_action.triggered.connect(self.clear_chat)
        menu.addAction(clear_action)

        if self.is_sleeping or self.sleep_transition:
            wake_action = QAction("叫醒", self)
            wake_action.triggered.connect(self.wake_up)
            menu.addAction(wake_action)
        else:
            sleep_action = QAction("哄宝睡觉", self)
            sleep_action.triggered.connect(self.start_sleep)
            menu.addAction(sleep_action)

        #walk_action = QAction("开始散步" if not self.walking else "停止散步", self)
        #walk_action.triggered.connect(self.toggle_walking)
        #menu.addAction(walk_action)

        menu.addSeparator()

        top_action = QAction("始终置顶", self)
        top_action.setCheckable(True)
        top_action.setChecked(self.config.always_on_top)
        top_action.triggered.connect(self.toggle_always_on_top)
        menu.addAction(top_action)

        settings_action = QAction("设置", self)
        settings_action.triggered.connect(self.open_settings)
        menu.addAction(settings_action)

        #reset_action = QAction("移到屏幕右下角", self)
        #reset_action.triggered.connect(self.reset_position)
        #menu.addAction(reset_action)

        menu.addSeparator()
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(QApplication.quit)
        menu.addAction(quit_action)
        menu.exec(global_pos)

    def focus_input(self) -> None:
        self.input_box.show()
        self.input_box.raise_()
        self.input_box.setFocus(Qt.MouseFocusReason)
        self.layout_overlay_widgets()

    def default_input_hover_rect(self) -> QRect:
        # 只使用输入框默认的单行尺寸作为鼠标触发区，避免整块透明窗口都触发。
        width = self.input_box.width()
        x = (self.width() - width) // 2
        y = self.pet_rect.bottom() + 8
        return QRect(x, y, width, 42)

    def refresh_hover_state(self) -> None:
        local = self.mapFromGlobal(QCursor.pos())
        bottom_hover = self.default_input_hover_rect()
        # 输入框只由人物下方固定热区触发；人物本体不再触发输入框。
        should_show = bottom_hover.contains(local)
        self.input_visible_by_hover = should_show

        if should_show or self.input_box.hasFocus() or self.input_box.toPlainText().strip():
            self.input_box.show()
            self.input_box.raise_()
            self.layout_overlay_widgets()
        else:
            self.input_box.hide()

    def enterEvent(self, event) -> None:
        self.refresh_hover_state()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        QTimer.singleShot(80, self.refresh_hover_state)
        super().leaveEvent(event)

    def open_settings(self) -> None:
        self.mark_activity()
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() == QDialog.Accepted:
            try:
                dialog.apply()
            except (OSError, RuntimeError) as exc:
                QMessageBox.critical(self, "保存设置失败", str(exc))
                return
            self.resize_for_pet_size()
            self.reset_auto_talk_countdown()
            self.update()

    def clear_chat(self) -> None:
        self.history.clear()
        self.reply_hide_timer.stop()
        self.typewriter_timer.stop()
        self.typewriter_full_text = ""
        self.typewriter_visible_text = ""
        self.typewriter_index = 0
        self.set_thinking(False)
        self.set_speaking(False)
        self.reply_bubble.hide()
        self.reply_bubble.clear()
        self.mark_activity()

    def send_user_message(self, text: str) -> None:
        self.mark_activity()
        if self.thread is not None:
            return
        self.history.append({"role": "user", "content": text})
        self.start_request(self.build_messages(), "user")

    def build_messages(self) -> list[dict]:
        messages = []
        if self.config.system_prompt:
            messages.append({"role": "system", "content": self.config.system_prompt})
        messages.extend(self.history[-20:])
        return messages

    def start_request(self, messages: list[dict], kind: str) -> None:
        if self.thread is not None:
            return
        self.request_kind = kind
        self.typewriter_timer.stop()
        self.set_speaking(False)
        self.set_thinking(True)
        self.show_reply("……", auto_hide=False)

        self.thread = QThread(self)
        self.worker = DeepSeekWorker(self.config, messages)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.finished.connect(self.on_reply)
        self.worker.failed.connect(self.on_error)
        self.worker.finished.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self.cleanup_thread)
        self.thread.start()

    def on_reply(self, text: str) -> None:
        if self.request_kind == "user":
            self.history.append({"role": "assistant", "content": text})
        self.set_thinking(False)
        self.start_typewriter_reply(text)
        self.reset_auto_talk_countdown()

    def on_error(self, message: str) -> None:
        self.set_thinking(False)
        self.set_speaking(False)
        self.show_reply("请求失败。请检查设置或网络。")
        QMessageBox.warning(self, "请求失败", message)
        self.reset_auto_talk_countdown()

    def cleanup_thread(self) -> None:
        if self.worker:
            self.worker.deleteLater()
        if self.thread:
            self.thread.deleteLater()
        self.worker = None
        self.thread = None

    def show_reply(self, text: str, auto_hide: bool = True) -> None:
        self.reply_hide_timer.stop()
        safe = html.escape(text).replace("\n", "<br>")
        self.reply_bubble.setText(safe)
        self.reply_bubble.show()
        self.reply_bubble.raise_()
        self.layout_overlay_widgets()
        if auto_hide:
            self.reply_hide_timer.start(max(1, self.config.reply_display_seconds) * 1000)

    def start_typewriter_reply(self, text: str) -> None:
        self.reply_hide_timer.stop()
        self.typewriter_timer.stop()
        self.typewriter_full_text = text or "……"
        self.typewriter_visible_text = ""
        self.typewriter_index = 0
        self.reply_bubble.clear()
        self.reply_bubble.show()
        self.reply_bubble.raise_()
        self.layout_overlay_widgets()
        self.set_speaking(True)
        self.typewriter_timer.start(TYPEWRITER_INTERVAL_MS)

    def reveal_next_character(self) -> None:
        if self.typewriter_index >= len(self.typewriter_full_text):
            self.finish_typewriter_reply()
            return

        self.typewriter_visible_text += self.typewriter_full_text[self.typewriter_index]
        self.typewriter_index += 1
        safe = html.escape(self.typewriter_visible_text).replace("\n", "<br>")
        self.reply_bubble.setText(safe)
        self.reply_bubble.show()
        self.reply_bubble.raise_()
        self.layout_overlay_widgets()

        if self.typewriter_index >= len(self.typewriter_full_text):
            self.finish_typewriter_reply()

    def finish_typewriter_reply(self) -> None:
        self.typewriter_timer.stop()
        self.set_speaking(False)
        self.reply_hide_timer.start(max(1, self.config.reply_display_seconds) * 1000)

    def hide_reply(self) -> None:
        self.reply_bubble.hide()
        self.reply_bubble.clear()

    def set_thinking(self, thinking: bool) -> None:
        self.is_thinking = thinking
        if thinking:
            self.walking = False
            self.wake_up()
            self.current_frame = "sleep1"
        elif not self.is_speaking and not self.is_dragging:
            self.current_frame = "idle"
        self.update()

    def set_speaking(self, speaking: bool) -> None:
        self.is_speaking = speaking
        if speaking:
            self.walking = False
            self.wake_up()
            self.current_frame = "speak1"
        elif not self.is_thinking and not self.is_dragging:
            self.current_frame = "idle"
        self.update()

    def toggle_walking(self) -> None:
        self.mark_activity()
        if self.is_thinking or self.is_speaking or self.is_dragging:
            return
        self.walking = not self.walking
        if self.walking:
            self.is_sleeping = False
            self.sleep_transition = False
        else:
            self.current_frame = "idle"

    def toggle_always_on_top(self, checked: bool) -> None:
        self.config.always_on_top = checked
        self.config.save()
        pos = self.pos()
        self.hide()
        self.apply_window_flags()
        self.show()
        self.move(pos)

    def reset_position(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 30, screen.bottom() - self.height() - 10)

    def start_sleep(self) -> None:
        if self.is_thinking or self.is_speaking or self.is_dragging:
            return
        self.walking = False
        self.sleep_transition = True
        self.is_sleeping = False
        self.current_frame = "sleep1"
        QTimer.singleShot(900, self.finish_sleep_transition)
        self.update()

    def finish_sleep_transition(self) -> None:
        if not self.sleep_transition:
            return
        self.sleep_transition = False
        self.is_sleeping = True
        self.current_frame = "sleep2"
        self.zzz_particles.clear()
        self.update()

    def wake_up(self) -> None:
        self.sleep_transition = False
        self.is_sleeping = False
        self.zzz_particles.clear()
        if not self.is_thinking and not self.is_speaking and not self.is_dragging:
            self.current_frame = "idle"
        self.update()

    def update_animation(self) -> None:
        if self.is_dragging:
            self.current_frame = "drag"
        elif self.is_thinking:
            self.current_frame = "sleep1"
        elif self.is_speaking:
            self.current_frame = "speak2" if self.current_frame == "speak1" else "speak1"
        elif self.walking and not self.is_sleeping and not self.sleep_transition:
            frames = ["walk1", "walk2", "walk3", "walk4"]
            self.current_frame = frames[self.walk_frame_index % len(frames)]
            self.walk_frame_index += 1
        elif not self.is_sleeping and not self.sleep_transition:
            self.current_frame = "idle"
        self.update()

    def update_movement(self) -> None:
        if (
            not self.walking
            or self.is_sleeping
            or self.sleep_transition
            or self.is_thinking
            or self.is_speaking
            or self.is_dragging
        ):
            return

        screen = QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        bounds = screen.availableGeometry()
        x = self.x() + self.walk_direction * self.config.walk_speed
        min_x = bounds.left()
        max_x = bounds.right() - self.width() + 1

        if x <= min_x:
            x = min_x
            self.walk_direction = 1
        elif x >= max_x:
            x = max_x
            self.walk_direction = -1

        self.facing_right = self.walk_direction > 0
        self.move(x, self.y())

    def update_behavior(self) -> None:
        self.last_activity_ms += 1000

        # 主动说话使用独立倒计时，不再被点击、拖动、输入等普通活动重置。
        # 请求、思考或逐字说话期间暂停；睡着时仍继续倒计时，到点后会自然醒来并搭话。
        if self.config.auto_talk and self.thread is None:
            if not self.is_thinking and not self.is_speaking and not self.is_dragging:
                self.auto_talk_remaining_ms -= 1000
                if self.auto_talk_remaining_ms <= 0:
                    prompt = (
                        "请以桌宠的身份，结合现在是你主动搭话的场景，随口对用户说一到两句自然、简短的话。"
                        "不要解释任务，不要使用标题，不要说和计算机相关的话题，也不要提到你收到了提示词。"
                    )
                    messages = []
                    if self.config.system_prompt:
                        messages.append({"role": "system", "content": self.config.system_prompt})
                    messages.append({"role": "user", "content": prompt})
                    self.start_request(messages, "auto")
                    return
        elif not self.config.auto_talk:
            # 开关关闭时保留一个有效倒计时；再次开启后从完整随机间隔开始。
            if self.auto_talk_remaining_ms <= 0:
                self.reset_auto_talk_countdown()

        if (
            not self.is_sleeping
            and not self.sleep_transition
            and not self.is_thinking
            and not self.is_speaking
            and not self.is_dragging
            and self.last_activity_ms >= self.sleep_after_ms
        ):
            self.start_sleep()
            return

        if self.is_sleeping:
            if random.random() < 0.004:
                self.mark_activity()
            return

        if (
            self.config.auto_walk
            and not self.walking
            and not self.is_thinking
            and not self.is_speaking
            and not self.is_dragging
            and random.random() < 0.035
        ):
            self.walking = True
            self.walk_direction = random.choice([-1, 1])
            QTimer.singleShot(random.randint(2500, 7000), self.stop_random_walk)

    def stop_random_walk(self) -> None:
        if (
            not self.is_thinking
            and not self.is_speaking
            and not self.is_sleeping
            and not self.is_dragging
        ):
            self.walking = False
            self.current_frame = "idle"
            self.update()

    def update_zzz(self) -> None:
        if not self.is_sleeping:
            return

        if random.random() < 0.12:
            self.zzz_particles.append(
                {
                    "x": self.pet_rect.left() + self.pet_rect.width() * random.uniform(0.48, 0.60),
                    "y": self.pet_rect.top() + self.pet_rect.height() * random.uniform(0.34, 0.43),
                    "opacity": 0.0,
                    "age": 0,
                }
            )

        alive = []
        for particle in self.zzz_particles:
            particle["age"] += 1
            particle["y"] -= self.config.pet_size * 0.007
            particle["x"] += math.sin(particle["age"] / 2) * 0.8
            if particle["age"] < 8:
                particle["opacity"] = particle["age"] / 8
            else:
                particle["opacity"] = max(0.0, 1.0 - (particle["age"] - 8) / 18)
            if particle["opacity"] > 0:
                alive.append(particle)

        self.zzz_particles = alive[-8:]
        self.update()


def ensure_config() -> None:
    if not CONFIG_PATH.exists():
        AppConfig().save()


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("萌萌桌宠")
    ensure_config()

    pet = PetWidget()
    pet.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
