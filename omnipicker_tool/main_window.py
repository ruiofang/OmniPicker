from __future__ import annotations

import sys
import re
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QThread, QTimer
from PyQt5.QtWidgets import QAbstractItemView
from PyQt5.QtWidgets import QApplication
from PyQt5.QtWidgets import QCheckBox
from PyQt5.QtWidgets import QComboBox
from PyQt5.QtWidgets import QDoubleSpinBox
from PyQt5.QtWidgets import QFileDialog
from PyQt5.QtWidgets import QFormLayout
from PyQt5.QtWidgets import QGridLayout
from PyQt5.QtWidgets import QGroupBox
from PyQt5.QtWidgets import QHBoxLayout
from PyQt5.QtWidgets import QLabel
from PyQt5.QtWidgets import QLineEdit
from PyQt5.QtWidgets import QHeaderView
from PyQt5.QtWidgets import QMainWindow
from PyQt5.QtWidgets import QMessageBox
from PyQt5.QtWidgets import QPushButton
from PyQt5.QtWidgets import QPlainTextEdit
from PyQt5.QtWidgets import QSpinBox
from PyQt5.QtWidgets import QScrollArea, QTabWidget
from PyQt5.QtWidgets import QTableWidget, QTableWidgetItem
from PyQt5.QtWidgets import QVBoxLayout
from PyQt5.QtWidgets import QWidget
from serial.tools import list_ports

from omnipicker_tool.can_service import CanMotionResult, OmniPickerCanService
from omnipicker_tool.constants import DEFAULT_BAUDRATES
from omnipicker_tool.constants import DEFAULT_SCAN_SLAVE_IDS
from omnipicker_tool.constants import DEFAULT_STOPBITS
from omnipicker_tool.constants import DEFAULT_TIMEOUT_SECONDS
from omnipicker_tool.constants import FAULT_CODE_LABELS
from omnipicker_tool.constants import MAX_AUTO_POLL_FAILURES
from omnipicker_tool.constants import STATE_LABELS
from omnipicker_tool.custom_service import OmniPickerCustomService
from omnipicker_tool.device_card import DeviceCard
from omnipicker_tool.modbus_service import ModbusServiceError
from omnipicker_tool.modbus_service import OmniPickerModbusService
from omnipicker_tool.protocol_service import DeviceStatus
from omnipicker_tool.protocol_service import SerialProtocolService


class CanOperation(QThread):
    """Keep interface authorization and CAN response waits off the GUI thread."""

    def __init__(self, operation: Callable[[], object], parent: QWidget) -> None:
        super().__init__(parent)
        self.operation = operation
        self.result: object = None
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            self.result = self.operation()
        except Exception as exc:
            self.error = exc


def parse_can_ids(text: str) -> list[int]:
    tokens = re.split(r"[\s,，;；]+", text.strip())
    ids: list[int] = []
    for token in tokens:
        if not token:
            continue
        try:
            if not re.fullmatch(r"(?:[0-9]+|0[xX][0-9a-fA-F]+)", token):
                raise ValueError
            node_id = int(token, 16 if token.lower().startswith("0x") else 10)
            if not 0 <= node_id <= 0x7FF:
                raise ValueError
        except ValueError as exc:
            raise ModbusServiceError(f"无效 CAN ID：{token}；请输入 0–2047 或 0x000–0x7FF") from exc
        if node_id not in ids:
            ids.append(node_id)
    if not ids:
        raise ModbusServiceError("请填写多 ID 控制的目标，例如 1, 2, 0x10")
    return ids


class ValueEditor(QWidget):
    def __init__(self, label_text: str, initial: int) -> None:
        super().__init__()
        self.label = QLabel(label_text)
        self.spin_box = QSpinBox()
        self.spin_box.setRange(0, 255)
        self.spin_box.setValue(initial)

        layout = QHBoxLayout(self)
        layout.addWidget(self.label)
        layout.addWidget(self.spin_box)

    def value(self) -> int:
        return self.spin_box.value()

    def set_value(self, value: int) -> None:
        self.spin_box.setValue(value)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.service: SerialProtocolService = OmniPickerCanService()
        self._can_worker: CanOperation | None = None
        self._can_success: Callable[[object], None] | None = None
        self._close_when_idle = False
        self.device_cards: list[DeviceCard] = []
        self._pending_card_ids: set[int] = set()
        self.setWindowTitle("OmniPicker CAN / RS485 Tool")
        self.resize(1080, 900)

        self.protocol_combo = QComboBox()
        self.port_combo = QComboBox()
        self.baudrate_combo = QComboBox()
        self.stopbits_combo = QComboBox()
        self.slave_id_spin = QSpinBox()
        self.slave_id_label = QLabel("目标 ID（十进制）")
        self.multi_id_checkbox = QCheckBox("多 ID 一起控制")
        self.can_ids_edit = QLineEdit()
        self.can_ids_edit.setPlaceholderText("例如 1, 2, 0x10（逗号或空格分隔）")
        self.can_ids_edit.setToolTip("勾选后，全开、全闭和发送运动均发给列表内所有 ID；不修改设备自身 ID。")
        self.timeout_spin = QDoubleSpinBox()
        self.retries_spin = QSpinBox()
        self.auto_poll_checkbox = QCheckBox("自动轮询")
        self.connect_button = QPushButton("连接")
        self.refresh_ports_button = QPushButton("刷新接口")
        self.scan_button = QPushButton("扫描常用参数")
        self.full_scan_button = QPushButton("全地址扫描")

        self.position_editor = ValueEditor("目标位置", 127)
        self.speed_editor = ValueEditor("目标速度", 255)
        self.force_editor = ValueEditor("目标力矩", 255)
        self.acceleration_editor = ValueEditor("目标加速度", 255)
        self.deceleration_editor = ValueEditor("目标减速度", 255)
        self.open_button = QPushButton("全开")
        self.close_button = QPushButton("全闭")
        self.send_button = QPushButton("发送运动")
        self.read_button = QPushButton("读取状态")

        self.firmware_label = QLabel("-")
        self.fault_label = QLabel("-")
        self.state_label = QLabel("-")
        self.position_label = QLabel("-")
        self.speed_label = QLabel("-")
        self.force_label = QLabel("-")
        self.updated_at_label = QLabel("-")
        self.status_id_label = QLabel("-")
        self.can_status_table = QTableWidget(0, 8)
        self.can_status_table.setHorizontalHeaderLabels(["CAN ID", "回执", "错误码", "状态", "位置", "速度", "力矩", "刷新时间"])
        self.can_status_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.can_status_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.can_status_table.horizontalHeader().setStretchLastSection(True)
        self.can_status_table.setMinimumHeight(110)
        self.can_status_table.setMaximumHeight(180)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(500)
        self.poll_timer.timeout.connect(self.poll_status)
        self._poll_failure_count = 0

        self._setup_ui()
        self._bind_events()
        self.refresh_ports()
        self._set_connected(False)

    def _setup_ui(self) -> None:
        central = QWidget()
        root_layout = QVBoxLayout(central)

        connection_group = QGroupBox("连接设置")
        connection_layout = QGridLayout(connection_group)
        connection_layout.setColumnStretch(1, 1)
        connection_layout.setColumnStretch(3, 1)
        self.slave_id_spin.setRange(1, 247)
        self.slave_id_spin.setValue(1)
        self.timeout_spin.setRange(0.05, 5.0)
        self.timeout_spin.setDecimals(2)
        self.timeout_spin.setSingleStep(0.05)
        self.timeout_spin.setValue(DEFAULT_TIMEOUT_SECONDS)
        self.retries_spin.setRange(0, 10)
        self.retries_spin.setValue(0)
        self.auto_poll_checkbox.setChecked(True)
        self.protocol_combo.addItem("Modbus RTU", "modbus")
        self.protocol_combo.addItem("自定义协议", "custom")
        self.protocol_combo.addItem("CAN (SocketCAN)", "can")
        self.protocol_combo.addItem("CAN-FD (SocketCAN)", "canfd")
        self.protocol_combo.setCurrentIndex(self.protocol_combo.findData("can"))

        for baudrate in DEFAULT_BAUDRATES:
            self.baudrate_combo.addItem(str(baudrate), baudrate)
        self.baudrate_combo.setCurrentText("115200")
        self.baudrate_combo.setEditable(True)

        for stopbits in DEFAULT_STOPBITS:
            self.stopbits_combo.addItem(str(stopbits), stopbits)
        self.stopbits_combo.setCurrentText("1")

        button_bar = QHBoxLayout()
        button_bar.addWidget(self.refresh_ports_button)
        button_bar.addWidget(self.scan_button)
        button_bar.addWidget(self.full_scan_button)
        button_bar.addWidget(self.connect_button)

        connection_layout.addWidget(QLabel("通信接口"), 0, 0)
        connection_layout.addWidget(self.port_combo, 0, 1)
        connection_layout.addWidget(QLabel("协议"), 0, 2)
        connection_layout.addWidget(self.protocol_combo, 0, 3, 1, 3)
        connection_layout.addWidget(self.slave_id_label, 1, 0)
        connection_layout.addWidget(self.slave_id_spin, 1, 1)
        connection_layout.addWidget(QLabel("超时(秒)"), 1, 2)
        connection_layout.addWidget(self.timeout_spin, 1, 3)
        connection_layout.addWidget(QLabel("重试次数"), 1, 4)
        connection_layout.addWidget(self.retries_spin, 1, 5)
        self.serial_settings_widget = QWidget()
        serial_settings = QHBoxLayout(self.serial_settings_widget)
        serial_settings.setContentsMargins(0, 0, 0, 0)
        serial_settings.addWidget(QLabel("波特率"))
        serial_settings.addWidget(self.baudrate_combo)
        serial_settings.addWidget(QLabel("停止位"))
        serial_settings.addWidget(self.stopbits_combo)
        serial_settings.addWidget(self.auto_poll_checkbox)
        connection_layout.addWidget(self.serial_settings_widget, 2, 0, 1, 6)
        self.can_targets_widget = QWidget()
        targets_layout = QHBoxLayout(self.can_targets_widget)
        targets_layout.setContentsMargins(0, 0, 0, 0)
        targets_layout.addWidget(self.multi_id_checkbox)
        targets_layout.addWidget(self.can_ids_edit)
        connection_layout.addLayout(button_bar, 3, 0, 1, 6)

        motion_group = QGroupBox("运动控制")
        motion_layout = QGridLayout(motion_group)
        motion_layout.addWidget(self.position_editor, 0, 0)
        motion_layout.addWidget(self.speed_editor, 0, 1)
        motion_layout.addWidget(self.force_editor, 1, 0)
        motion_layout.addWidget(self.acceleration_editor, 1, 1)
        motion_layout.addWidget(self.deceleration_editor, 2, 0)

        action_bar = QHBoxLayout()
        action_bar.addWidget(self.close_button)
        action_bar.addWidget(self.open_button)
        action_bar.addWidget(self.send_button)
        action_bar.addWidget(self.read_button)
        motion_layout.addLayout(action_bar, 2, 1)

        status_group = QGroupBox("状态反馈")
        status_layout = QFormLayout(status_group)
        status_layout.addRow("反馈目标", self.status_id_label)
        status_layout.addRow("固件版本寄存器", self.firmware_label)
        status_layout.addRow("错误码", self.fault_label)
        status_layout.addRow("当前状态", self.state_label)
        status_layout.addRow("当前位置", self.position_label)
        status_layout.addRow("当前速度", self.speed_label)
        status_layout.addRow("当前扭矩", self.force_label)
        status_layout.addRow("最近刷新", self.updated_at_label)
        status_layout.addRow(self.can_status_table)

        log_group = QGroupBox("日志")
        log_layout = QVBoxLayout(log_group)
        log_layout.addWidget(self.log_view)

        self.control_tabs = QTabWidget()
        common_page = QWidget()
        common_layout = QVBoxLayout(common_page)
        common_layout.addWidget(self.can_targets_widget)
        common_layout.addWidget(motion_group)
        common_layout.addWidget(status_group)
        common_scroll = QScrollArea()
        common_scroll.setWidgetResizable(True)
        common_scroll.setWidget(common_page)
        self.control_tabs.addTab(common_scroll, "统一控制")
        self.cards_page = QWidget()
        cards_layout = QVBoxLayout(self.cards_page)
        card_toolbar = QHBoxLayout()
        self.add_device_button = QPushButton("添加设备")
        self.load_devices_button = QPushButton("导入配置")
        self.save_devices_button = QPushButton("保存配置")
        self.cards_close_button = QPushButton("勾选设备全闭")
        self.cards_open_button = QPushButton("勾选设备全开")
        self.cards_send_button = QPushButton("下发勾选设备参数")
        for button in (self.add_device_button, self.load_devices_button, self.save_devices_button,
                       self.cards_close_button, self.cards_open_button, self.cards_send_button):
            card_toolbar.addWidget(button)
        cards_layout.addLayout(card_toolbar)
        cards_layout.addWidget(QLabel("每张卡片独立设置 ID 和参数；批量下发使用各卡片的参数，共用上方 CAN 连接。"))
        self.cards_scroll = QScrollArea()
        self.cards_scroll.setWidgetResizable(True)
        cards_container = QWidget()
        self.cards_grid = QGridLayout(cards_container)
        self.cards_grid.setColumnStretch(0, 1)
        self.cards_grid.setColumnStretch(1, 1)
        self.cards_scroll.setWidget(cards_container)
        cards_layout.addWidget(self.cards_scroll)
        self.control_tabs.addTab(self.cards_page, "设备卡片")
        self.add_device_card(1)
        self.add_device_card(2)
        root_layout.addWidget(connection_group)
        root_layout.addWidget(self.control_tabs, 1)
        log_group.setMaximumHeight(155)
        root_layout.addWidget(log_group)

        self.setCentralWidget(central)

    def _bind_events(self) -> None:
        self.refresh_ports_button.clicked.connect(self.refresh_ports)
        self.connect_button.clicked.connect(self.toggle_connection)
        self.scan_button.clicked.connect(self.scan_common_settings)
        self.full_scan_button.clicked.connect(self.scan_all_slave_ids)
        self.protocol_combo.currentIndexChanged.connect(self._handle_protocol_changed)
        self.slave_id_spin.valueChanged.connect(self._handle_target_changed)
        self.multi_id_checkbox.toggled.connect(self._update_protocol_controls)
        self.open_button.clicked.connect(lambda: self._quick_move(255))
        self.close_button.clicked.connect(lambda: self._quick_move(0))
        self.send_button.clicked.connect(self.send_motion)
        self.read_button.clicked.connect(self.poll_status)
        self.add_device_button.clicked.connect(lambda: self.add_device_card())
        self.save_devices_button.clicked.connect(self.save_device_config)
        self.load_devices_button.clicked.connect(self.load_device_config)
        self.cards_send_button.clicked.connect(lambda: self.send_device_cards())
        self.cards_open_button.clicked.connect(lambda: self.send_device_cards(position=255))
        self.cards_close_button.clicked.connect(lambda: self.send_device_cards(position=0))

    def add_device_card(self, node_id: int | None = None, name: str = "") -> DeviceCard | None:
        if self._can_worker is not None:
            return None
        if node_id is None:
            used_ids = {card.node_id for card in self.device_cards}
            node_id = next((item for item in range(1, 0x800) if item not in used_ids), None)
            if node_id is None:
                self._show_error("没有可用的 CAN ID")
                return None
        card = DeviceCard(node_id, name)
        card.motion_requested.connect(lambda device: self.send_device_cards([device]))
        card.remove_requested.connect(self.remove_device_card)
        self.device_cards.append(card)
        index = len(self.device_cards) - 1
        self.cards_grid.addWidget(card, index // 2, index % 2)
        card.set_controls(self.service.is_connected and self._selected_protocol() in {"can", "canfd"}, False)
        return card

    def remove_device_card(self, card: DeviceCard) -> None:
        if self._can_worker is not None or card not in self.device_cards:
            return
        self.device_cards.remove(card)
        self.cards_grid.removeWidget(card)
        card.deleteLater()
        for index, device in enumerate(self.device_cards):
            self.cards_grid.addWidget(device, index // 2, index % 2)

    def send_device_cards(self, cards: list[DeviceCard] | None = None, position: int | None = None) -> None:
        if self._can_worker is not None:
            return
        service = self.service
        if not isinstance(service, OmniPickerCanService) or not service.is_connected:
            self._show_error("请先连接 CAN 接口")
            return
        targets = cards if cards is not None else [card for card in self.device_cards if card.selected_checkbox.isChecked()]
        if not targets:
            self._show_error("请先勾选要控制的设备卡片")
            return
        node_ids = [card.node_id for card in targets]
        if len(set(node_ids)) != len(node_ids):
            self._show_error("所选卡片的 CAN ID 重复，请为每个设备设置不同的 ID")
            return
        if position is not None:
            for card in targets:
                card.parameters["position"].setValue(position)
        commands = {card.node_id: card.motion_values() for card in targets}
        self._prepare_can_results(node_ids)
        self._pending_card_ids = set(node_ids)
        for card in self.device_cards:
            if card.node_id in commands:
                card.mark_pending()
        for card in targets:
            self._log(f"设备 {card.name_edit.text()} · CAN ID {card.node_id} 下发："
                      f"pos/speed/force/acc/dec={commands[card.node_id]}")
        self._start_can_operation(
            lambda: service.write_motion_commands(commands),
            lambda result: self._can_motion_complete(node_ids, result),
        )

    def save_device_config(self) -> None:
        if self._can_worker is not None:
            return
        node_ids = [card.node_id for card in self.device_cards]
        if len(set(node_ids)) != len(node_ids):
            self._show_error("设备卡片的 CAN ID 重复，请修改后再保存配置")
            return
        path, _filter = QFileDialog.getSaveFileName(self, "保存设备卡片配置", "omnipicker_devices.json", "JSON 配置 (*.json)")
        if not path:
            return
        try:
            Path(path).write_text(json.dumps({"version": 1, "devices": [card.configuration() for card in self.device_cards]},
                                            ensure_ascii=False, indent=2), encoding="utf-8")
            self._log(f"已保存 {len(self.device_cards)} 张设备卡片配置：{path}")
        except OSError as exc:
            self._show_error(f"保存设备配置失败：{exc}")

    def load_device_config(self) -> None:
        if self._can_worker is not None:
            return
        path, _filter = QFileDialog.getOpenFileName(self, "导入设备卡片配置", "", "JSON 配置 (*.json)")
        if not path:
            return
        try:
            config = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(config, dict) or config.get("version") != 1 or not isinstance(config.get("devices"), list):
                raise ValueError("配置格式不正确")
            devices = config["devices"]
            ids = set()
            for device in devices:
                if not isinstance(device, dict) or type(device.get("id")) is not int or not 0 <= device["id"] <= 0x7FF:
                    raise ValueError("CAN ID 应为 0–2047 的整数")
                if device["id"] in ids:
                    raise ValueError("设备配置中有重复 CAN ID")
                ids.add(device["id"])
                if not isinstance(device.get("name"), str) or type(device.get("selected")) is not bool:
                    raise ValueError("设备名称或勾选状态格式不正确")
                parameters = device.get("parameters")
                if not isinstance(parameters, dict) or set(parameters) != {"position", "speed", "force", "acceleration", "deceleration"}:
                    raise ValueError("缺少运动参数")
                if any(type(value) is not int or not 0 <= value <= 255 for value in parameters.values()):
                    raise ValueError("运动参数应为 0–255 的整数")
        except (OSError, ValueError) as exc:
            self._show_error(f"导入设备配置失败：{exc}")
            return
        for card in list(self.device_cards):
            self.remove_device_card(card)
        for device in devices:
            card = self.add_device_card(device["id"], device["name"])
            card.selected_checkbox.setChecked(device["selected"])
            for key, value in device["parameters"].items():
                card.parameters[key].setValue(value)
        self._log(f"已导入 {len(devices)} 张设备卡片配置")

    def refresh_ports(self) -> None:
        current_port = self.port_combo.currentText()
        self.port_combo.clear()
        if self._selected_protocol() in {"can", "canfd"}:
            interfaces = self._socketcan_interfaces()
            for interface in interfaces:
                self.port_combo.addItem(interface, interface)
            if not interfaces:
                self.port_combo.addItem("未发现 SocketCAN 接口", "")
            if current_port:
                index = self.port_combo.findData(current_port)
                if index >= 0:
                    self.port_combo.setCurrentIndex(index)
            return

        ports = sorted(list_ports.comports(), key=lambda item: item.device)
        for port in ports:
            label = f"{port.device} - {port.description}"
            self.port_combo.addItem(label, port.device)
        if not ports:
            self.port_combo.addItem("未发现串口", "")
        if current_port:
            index = self.port_combo.findData(current_port)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)

    def toggle_connection(self) -> None:
        if self._can_worker is not None:
            return
        if self.service.is_connected:
            self.service.disconnect()
            self.poll_timer.stop()
            self._set_connected(False)
            self._clear_status()
            self.can_status_table.setRowCount(0)
            for card in self.device_cards:
                card.clear_status()
            self._log("接口已断开")
            return

        self._reset_service_for_protocol()

        port = self.port_combo.currentData()
        if not port:
            self._show_error("请先选择可用通信接口")
            return

        try:
            baudrate = self._selected_baudrate()
            stopbits = self._selected_stopbits()
        except ValueError:
            self._show_error("波特率或停止位格式无效")
            return

        parameters = dict(port=port, baudrate=baudrate, slave_id=self.slave_id_spin.value(),
                          timeout=self.timeout_spin.value(), stopbits=stopbits, retries=self.retries_spin.value())
        if self._selected_protocol() in {"can", "canfd"}:
            self._log(f"正在配置并连接 {port}；需要管理员权限时请完成系统授权。连接本身不会发送运动命令。")
            self._start_can_operation(
                lambda: self.service.connect(**parameters),
                lambda _result: self._connection_ready(port, baudrate, stopbits),
            )
            return
        try:
            self.service.connect(**parameters)
            self._connection_ready(port, baudrate, stopbits)
        except ModbusServiceError as exc:
            self._show_error(str(exc))
            self._sync_disconnected_state()

    def _connection_ready(self, port: str, baudrate: int, stopbits: float) -> None:
        self._poll_failure_count = 0
        self._clear_status()
        self.can_status_table.setRowCount(0)
        for card in self.device_cards:
            card.clear_status()
        self._set_connected(True)
        self._log(self._connection_log(port, baudrate, stopbits))
        if self.auto_poll_checkbox.isChecked() and self._selected_protocol() == "modbus":
            self.poll_timer.start()
            self.poll_status()
        elif self._selected_protocol() in {"custom", "can", "canfd"}:
            self._log(f"{self._protocol_label()} 不支持独立轮询；设备在收到控制帧后会立即返回一帧状态。")

    def send_motion(self) -> None:
        if self._can_worker is not None:
            return
        if self._selected_protocol() in {"can", "canfd"}:
            self._send_can_motion()
            return
        try:
            status = self.service.write_motion(
                position=self.position_editor.value(),
                speed=self.speed_editor.value(),
                force=self.force_editor.value(),
                acceleration=self.acceleration_editor.value(),
                deceleration=self.deceleration_editor.value(),
            )
            self._log(
                "已写入运动命令: "
                f"pos={self.position_editor.value()} speed={self.speed_editor.value()} "
                f"force={self.force_editor.value()} acc={self.acceleration_editor.value()} "
                f"dec={self.deceleration_editor.value()} trigger=1"
            )
            if status is not None:
                self._update_status(status)
            else:
                self.poll_status()
        except ModbusServiceError as exc:
            self._show_error(str(exc))
            self._sync_disconnected_state()

    def _send_can_motion(self) -> None:
        service = self.service
        if not isinstance(service, OmniPickerCanService):
            return
        try:
            node_ids = parse_can_ids(self.can_ids_edit.text()) if self.multi_id_checkbox.isChecked() else [self.slave_id_spin.value()]
        except ModbusServiceError as exc:
            self._show_error(str(exc))
            return
        parameters = dict(position=self.position_editor.value(), speed=self.speed_editor.value(),
                          force=self.force_editor.value(), acceleration=self.acceleration_editor.value(),
                          deceleration=self.deceleration_editor.value())
        self._prepare_can_results(node_ids)
        self._pending_card_ids = set(node_ids)
        for card in self.device_cards:
            if card.node_id in node_ids:
                card.mark_pending()
        self._log(f"向 CAN ID {', '.join(str(item) for item in node_ids)} 下发运动命令："
                  f"pos={parameters['position']} speed={parameters['speed']} force={parameters['force']} "
                  f"acc={parameters['acceleration']} dec={parameters['deceleration']}")
        self._start_can_operation(
            lambda: service.write_motion_many(node_ids=node_ids, **parameters),
            lambda result: self._can_motion_complete(node_ids, result),
        )

    def _prepare_can_results(self, node_ids: list[int]) -> None:
        self._clear_status()
        self.can_status_table.setRowCount(len(node_ids))
        for row, node_id in enumerate(node_ids):
            for column, value in enumerate([f"{node_id} (0x{node_id:X})", "等待回执", "-", "-", "-", "-", "-", "-"]):
                self.can_status_table.setItem(row, column, QTableWidgetItem(value))

    def _can_motion_complete(self, node_ids: list[int], result: CanMotionResult) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        for row, node_id in enumerate(node_ids):
            status = result.statuses.get(node_id)
            if status is not None:
                values = ["已收到", self._describe_code(status.fault_code, FAULT_CODE_LABELS),
                          self._describe_code(status.state, STATE_LABELS), str(status.position),
                          str(status.speed), str(status.force), now]
                self._log(f"CAN ID {node_id} (0x{node_id:X}) 回执：pos={status.position} speed={status.speed} "
                          f"force={status.force} fault=0x{status.fault_code:02X}")
                if node_id == self.slave_id_spin.value():
                    self._update_status(status)
            else:
                error = result.errors.get(node_id, "未收到回执")
                values = ["失败（详见日志）", "-", "-", "-", "-", "-", now]
                self._log(f"CAN ID {node_id} (0x{node_id:X})：{error}")
            for card in self.device_cards:
                if card.node_id == node_id:
                    if status is not None:
                        card.show_status(status)
                    else:
                        card.show_error(error)
            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(value)
                if status is None:
                    item.setToolTip(error)
                self.can_status_table.setItem(row, column, item)
        if result.errors:
            self._log(f"本批次 {len(result.statuses)}/{len(node_ids)} 个 ID 返回状态。无回执不代表运动命令未执行。")
        self._sync_disconnected_state()

    def _start_can_operation(self, operation: Callable[[], object], on_success: Callable[[object], None]) -> None:
        if self._can_worker is not None:
            return
        self._can_success = on_success
        self._can_worker = CanOperation(operation, self)
        self._can_worker.finished.connect(self._finish_can_operation)
        self._set_connected(self.service.is_connected)
        self._can_worker.start()

    def _finish_can_operation(self) -> None:
        worker = self._can_worker
        if worker is None:
            return
        on_success = self._can_success
        self._can_worker = None
        self._can_success = None
        try:
            if worker.error is not None:
                self._log(str(worker.error))
                for row in range(self.can_status_table.rowCount()):
                    self.can_status_table.setItem(row, 1, QTableWidgetItem("失败（详见日志）"))
                for card in self.device_cards:
                    if card.node_id in self._pending_card_ids:
                        card.show_error(str(worker.error))
                if not self._close_when_idle:
                    self._show_error(str(worker.error))
            elif on_success is not None:
                on_success(worker.result)
        finally:
            self._pending_card_ids.clear()
            worker.deleteLater()
            self._set_connected(self.service.is_connected)
            if self._close_when_idle:
                self.close()

    def _handle_target_changed(self) -> None:
        if self._selected_protocol() in {"can", "canfd"} and isinstance(self.service, OmniPickerCanService):
            self.service.set_node_id(self.slave_id_spin.value())
            self._clear_status()
            if self.service.is_connected:
                self._log(f"当前单目标已切换为 CAN ID {self.slave_id_spin.value()}，下一次发送生效。")

    def _clear_status(self) -> None:
        for label in (self.status_id_label, self.firmware_label, self.fault_label, self.state_label,
                      self.position_label, self.speed_label, self.force_label, self.updated_at_label):
            label.setText("-")

    def poll_status(self) -> None:
        if not self.service.is_connected:
            return

        if self._selected_protocol() in {"custom", "can", "canfd"} and self.poll_timer.isActive():
            self.poll_timer.stop()
            self._log("自定义协议没有独立读状态命令，已停止自动轮询。请通过发送运动命令获取状态回执。")
            return

        try:
            status = self.service.read_status()
        except ModbusServiceError as exc:
            if self._sync_disconnected_state():
                self._log(f"状态刷新失败，串口连接已失效: {exc}")
                return
            self._poll_failure_count += 1
            self._log(f"状态刷新失败: {exc}")
            if self.poll_timer.isActive() and self._poll_failure_count >= MAX_AUTO_POLL_FAILURES:
                self.poll_timer.stop()
                self._log(
                    "连续读取失败，已暂停自动轮询。"
                    f"请优先检查 {self._protocol_hint()}、波特率、停止位和从站地址。"
                )
            return

        self._poll_failure_count = 0
        self._update_status(status)

    def _update_status(self, status: DeviceStatus) -> None:
        self.status_id_label.setText(str(self.slave_id_spin.value()))
        firmware_text = "-" if status.firmware_version is None else str(status.firmware_version)
        self.firmware_label.setText(firmware_text)
        self.fault_label.setText(self._describe_code(status.fault_code, FAULT_CODE_LABELS))
        self.state_label.setText(self._describe_code(status.state, STATE_LABELS))
        self.position_label.setText(self._format_u8(status.position))
        self.speed_label.setText(self._format_u8(status.speed))
        self.force_label.setText(self._format_u8(status.force))
        self.updated_at_label.setText(datetime.now().strftime("%H:%M:%S"))

    def _sync_disconnected_state(self) -> bool:
        if self.service.is_connected:
            return False
        self.poll_timer.stop()
        self._set_connected(False)
        return True

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._can_worker is not None:
            self._close_when_idle = True
            self._log("正在等待当前操作结束后关闭；如有系统授权窗口，可取消授权。")
            event.ignore()
            return
        self.poll_timer.stop()
        self.service.disconnect()
        super().closeEvent(event)

    def _quick_move(self, position: int) -> None:
        self.position_editor.set_value(position)
        self.send_motion()

    def scan_common_settings(self) -> None:
        self._scan_settings(full_address_scan=False)

    def scan_all_slave_ids(self) -> None:
        self._scan_settings(full_address_scan=True)

    def _scan_settings(self, full_address_scan: bool) -> None:
        if self._selected_protocol() in {"can", "canfd"}:
            self._show_error("CAN 模式无需扫描串口参数，请先在系统中配置 SocketCAN 接口")
            return
        if self.service.is_connected:
            self.service.disconnect()
            self.poll_timer.stop()
            self._set_connected(False)

        self._reset_service_for_protocol()

        port = self.port_combo.currentData()
        if not port:
            self._show_error("请先选择可用串口")
            return

        try:
            current_baudrate = self._selected_baudrate()
            current_stopbits = self._selected_stopbits()
        except ValueError:
            self._show_error("波特率或停止位格式无效")
            return

        baudrate_candidates = [current_baudrate] + [item for item in DEFAULT_BAUDRATES if item != current_baudrate]
        stopbits_candidates = [current_stopbits] + [item for item in DEFAULT_STOPBITS if item != current_stopbits]
        if full_address_scan:
            slave_id_candidates = [self.slave_id_spin.value()] + [item for item in range(1, 248) if item != self.slave_id_spin.value()]
            scan_label = "全地址"
        else:
            slave_id_candidates = [self.slave_id_spin.value()] + [item for item in DEFAULT_SCAN_SLAVE_IDS if item != self.slave_id_spin.value()]
            scan_label = "常用参数"

        self._log(
            f"开始{scan_label}扫描 {port}，协议 {self._protocol_label()}，波特率 {baudrate_candidates}，停止位 {stopbits_candidates}，"
            f"从站范围 {slave_id_candidates[0]}-{slave_id_candidates[-1]}，共 {len(slave_id_candidates)} 个地址"
        )

        for baudrate in baudrate_candidates:
            for stopbits in stopbits_candidates:
                for slave_id in slave_id_candidates:
                    try:
                        self.service.connect(
                            port=port,
                            baudrate=baudrate,
                            slave_id=slave_id,
                            timeout=min(self.timeout_spin.value(), 0.2),
                            stopbits=stopbits,
                            retries=0,
                        )
                        fault_code = self.service.probe()
                    except ModbusServiceError:
                        self.service.disconnect()
                        continue

                    self.service.disconnect()
                    self.baudrate_combo.setCurrentText(str(baudrate))
                    self.stopbits_combo.setCurrentText(str(stopbits))
                    self.slave_id_spin.setValue(slave_id)
                    self._log(
                        f"扫描命中: 波特率 {baudrate}，停止位 {stopbits}，从站 {slave_id}，错误码 {fault_code}"
                    )
                    QMessageBox.information(
                        self,
                        "扫描成功",
                        f"命中参数: 波特率 {baudrate}, 停止位 {stopbits}, 从站 {slave_id}",
                    )
                    return

        self._log(
            f"{scan_label}扫描未命中。请确认设备已切到 RS485，{self._protocol_hint()}，"
            "并核对上位机中的 uart_baudrate、uart_stopbits、can_node_id，"
            "同时检查 485 A/B 是否接反、GND 是否共地。"
        )
        QMessageBox.warning(
            self,
            "扫描未命中",
            "未在扫描参数组合中收到设备响应，请检查设备侧协议、地址和 485 接线。",
        )

    def _handle_protocol_changed(self) -> None:
        if self._can_worker is not None:
            return
        self.poll_timer.stop()
        self._clear_status()
        self.can_status_table.setRowCount(0)
        for card in self.device_cards:
            card.clear_status()
        self._set_connected(False)
        self._reset_service_for_protocol()
        self.refresh_ports()
        if self._selected_protocol() == "custom":
            self.auto_poll_checkbox.setChecked(False)
            self._log("已切换到自定义协议。该模式兼容 modbus_mode=False 或 3.3.5 之前固件的 485 自定义帧。")
        else:
            if self._selected_protocol() == "modbus":
                self.auto_poll_checkbox.setChecked(True)
                self._log("已切换到 Modbus RTU。该模式要求设备侧 modbus_mode=True。")
            else:
                self.auto_poll_checkbox.setChecked(False)
                self._log(
                    f"已切换到 {self._protocol_label()}。CAN ID 复用 can_node_id；"
                    "连接时自动配置并启用 SocketCAN；需要权限时会弹出系统授权。"
                )
        self._update_protocol_controls()

    def _reset_service_for_protocol(self) -> None:
        protocol = self._selected_protocol()
        if protocol == "custom":
            if not isinstance(self.service, OmniPickerCustomService):
                self.service.disconnect()
                self.service = OmniPickerCustomService()
        elif protocol == "modbus":
            if not isinstance(self.service, OmniPickerModbusService):
                self.service.disconnect()
                self.service = OmniPickerModbusService()
        else:
            use_fd = protocol == "canfd"
            if not isinstance(self.service, OmniPickerCanService) or self.service.is_fd != use_fd:
                self.service.disconnect()
                self.service = OmniPickerCanService(is_fd=use_fd)

    def _set_connected(self, connected: bool) -> None:
        busy = self._can_worker is not None
        self.connect_button.setText("正在处理…" if busy else ("断开" if connected else "连接"))
        self.connect_button.setEnabled(not busy)
        self.send_button.setEnabled(connected and not busy)
        self.read_button.setEnabled(connected and not busy and self._selected_protocol() == "modbus")
        self.open_button.setEnabled(connected and not busy)
        self.close_button.setEnabled(connected and not busy)
        for editor in (self.position_editor, self.speed_editor, self.force_editor,
                       self.acceleration_editor, self.deceleration_editor):
            editor.setEnabled(not busy)
        self._update_protocol_controls()

    def _update_protocol_controls(self) -> None:
        is_can = self._selected_protocol() in {"can", "canfd"}
        connected = self.service.is_connected
        idle_disconnected = not connected and self._can_worker is None
        busy = self._can_worker is not None
        self.slave_id_spin.setRange(0 if is_can else 1, 0x7FF if is_can else 247)
        self.slave_id_label.setText("单目标 CAN ID（十进制）" if is_can else "从站地址")
        self.slave_id_spin.setEnabled(not busy and (is_can or not connected))
        self.can_targets_widget.setVisible(is_can)
        self.serial_settings_widget.setVisible(not is_can)
        self.can_status_table.setVisible(is_can)
        self.control_tabs.setTabEnabled(1, is_can)
        if not is_can and self.control_tabs.currentIndex() == 1:
            self.control_tabs.setCurrentIndex(0)
        self.add_device_button.setEnabled(is_can and not busy)
        self.load_devices_button.setEnabled(is_can and not busy)
        self.save_devices_button.setEnabled(is_can and not busy)
        for button in (self.cards_close_button, self.cards_open_button, self.cards_send_button):
            button.setEnabled(is_can and connected and not busy)
        for card in self.device_cards:
            card.set_controls(is_can and connected, busy)
        self.multi_id_checkbox.setEnabled(not busy)
        self.can_ids_edit.setEnabled(not busy and self.multi_id_checkbox.isChecked())
        self.protocol_combo.setEnabled(not busy)
        self.port_combo.setEnabled(idle_disconnected)
        self.refresh_ports_button.setEnabled(idle_disconnected)
        self.timeout_spin.setEnabled(idle_disconnected)
        self.retries_spin.setEnabled(idle_disconnected)
        self.baudrate_combo.setEnabled(not is_can and idle_disconnected)
        self.stopbits_combo.setEnabled(not is_can and idle_disconnected)
        self.auto_poll_checkbox.setEnabled(self._selected_protocol() == "modbus" and idle_disconnected)
        self.scan_button.setEnabled(not is_can and idle_disconnected)
        self.full_scan_button.setEnabled(not is_can and idle_disconnected)
        self.scan_button.setVisible(not is_can)
        self.full_scan_button.setVisible(not is_can)
        if is_can:
            self.auto_poll_checkbox.setChecked(False)

    def _selected_baudrate(self) -> int:
        return int(self.baudrate_combo.currentText())

    def _selected_protocol(self) -> str:
        return str(self.protocol_combo.currentData())

    def _selected_stopbits(self) -> float:
        return float(self.stopbits_combo.currentText())

    def _protocol_label(self) -> str:
        return self.protocol_combo.currentText()

    def _protocol_hint(self) -> str:
        if self._selected_protocol() == "custom":
            return "若为新固件请确认 modbus_mode=False；若为旧固件请确认设备使用 485 自定义协议"
        if self._selected_protocol() == "modbus":
            return "请确认 rs485_mode=1 且 modbus_mode=True"
        return "请确认 CAN 接口已启用、波特率正确、CAN_H/CAN_L 接线正确且节点 ID 匹配"

    def _connection_log(self, port: str, baudrate: int, stopbits: float) -> str:
        if self._selected_protocol() in {"can", "canfd"}:
            return (
                f"已连接 {port}，协议 {self._protocol_label()}，CAN ID {self.slave_id_spin.value()}，"
                f"超时 {self.timeout_spin.value():.2f}s，重试 {self.retries_spin.value()}"
            )
        return (
            f"已连接 {port}，协议 {self._protocol_label()}，波特率 {baudrate}，停止位 {stopbits}，"
            f"从站 {self.slave_id_spin.value()}，超时 {self.timeout_spin.value():.2f}s，"
            f"重试 {self.retries_spin.value()}"
        )

    @staticmethod
    def _socketcan_interfaces() -> list[str]:
        interfaces: list[str] = []
        net_root = Path("/sys/class/net")
        try:
            candidates = list(net_root.iterdir())
        except OSError:
            return interfaces
        for candidate in candidates:
            try:
                is_can = (candidate / "type").read_text().strip() == "280"
            except OSError:
                is_can = candidate.name.startswith(("can", "vcan", "slcan"))
            if is_can:
                interfaces.append(candidate.name)
        return sorted(interfaces)

    def _log(self, message: str) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{now}] {message}")

    def _show_error(self, message: str) -> None:
        QMessageBox.critical(self, "错误", message)
        self._log(message)

    @staticmethod
    def _format_u8(value: int) -> str:
        return f"{value} ({value / 255 * 100:.1f}%)"

    @staticmethod
    def _describe_code(value: int, labels: dict[int, str]) -> str:
        return f"0x{value:02X} - {labels.get(value, '未知') }"


def run() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
