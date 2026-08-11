from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication
from PyQt5.QtWidgets import QCheckBox
from PyQt5.QtWidgets import QComboBox
from PyQt5.QtWidgets import QDoubleSpinBox
from PyQt5.QtWidgets import QFormLayout
from PyQt5.QtWidgets import QGridLayout
from PyQt5.QtWidgets import QGroupBox
from PyQt5.QtWidgets import QHBoxLayout
from PyQt5.QtWidgets import QLabel
from PyQt5.QtWidgets import QMainWindow
from PyQt5.QtWidgets import QMessageBox
from PyQt5.QtWidgets import QPushButton
from PyQt5.QtWidgets import QPlainTextEdit
from PyQt5.QtWidgets import QSpinBox
from PyQt5.QtWidgets import QVBoxLayout
from PyQt5.QtWidgets import QWidget
from serial.tools import list_ports

from omnipicker_tool.can_service import OmniPickerCanService
from omnipicker_tool.constants import DEFAULT_BAUDRATES
from omnipicker_tool.constants import DEFAULT_SCAN_SLAVE_IDS
from omnipicker_tool.constants import DEFAULT_STOPBITS
from omnipicker_tool.constants import DEFAULT_TIMEOUT_SECONDS
from omnipicker_tool.constants import FAULT_CODE_LABELS
from omnipicker_tool.constants import MAX_AUTO_POLL_FAILURES
from omnipicker_tool.constants import STATE_LABELS
from omnipicker_tool.custom_service import OmniPickerCustomService
from omnipicker_tool.modbus_service import ModbusServiceError
from omnipicker_tool.modbus_service import OmniPickerModbusService
from omnipicker_tool.protocol_service import DeviceStatus
from omnipicker_tool.protocol_service import SerialProtocolService


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
        self.setWindowTitle("OmniPicker CAN / RS485 Tool")
        self.resize(900, 640)

        self.protocol_combo = QComboBox()
        self.port_combo = QComboBox()
        self.baudrate_combo = QComboBox()
        self.stopbits_combo = QComboBox()
        self.slave_id_spin = QSpinBox()
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
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)

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
        connection_layout = QFormLayout(connection_group)
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

        connection_layout.addRow("通信接口", self.port_combo)
        connection_layout.addRow("协议", self.protocol_combo)
        connection_layout.addRow("波特率", self.baudrate_combo)
        connection_layout.addRow("停止位", self.stopbits_combo)
        connection_layout.addRow("从站地址", self.slave_id_spin)
        connection_layout.addRow("超时(秒)", self.timeout_spin)
        connection_layout.addRow("重试次数", self.retries_spin)
        connection_layout.addRow("读取模式", self.auto_poll_checkbox)
        connection_layout.addRow(button_bar)

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
        status_layout.addRow("固件版本寄存器", self.firmware_label)
        status_layout.addRow("错误码", self.fault_label)
        status_layout.addRow("当前状态", self.state_label)
        status_layout.addRow("当前位置", self.position_label)
        status_layout.addRow("当前速度", self.speed_label)
        status_layout.addRow("当前扭矩", self.force_label)
        status_layout.addRow("最近刷新", self.updated_at_label)

        log_group = QGroupBox("日志")
        log_layout = QVBoxLayout(log_group)
        log_layout.addWidget(self.log_view)

        root_layout.addWidget(connection_group)
        root_layout.addWidget(motion_group)
        root_layout.addWidget(status_group)
        root_layout.addWidget(log_group)

        self.setCentralWidget(central)

    def _bind_events(self) -> None:
        self.refresh_ports_button.clicked.connect(self.refresh_ports)
        self.connect_button.clicked.connect(self.toggle_connection)
        self.scan_button.clicked.connect(self.scan_common_settings)
        self.full_scan_button.clicked.connect(self.scan_all_slave_ids)
        self.protocol_combo.currentIndexChanged.connect(self._handle_protocol_changed)
        self.open_button.clicked.connect(lambda: self._quick_move(255))
        self.close_button.clicked.connect(lambda: self._quick_move(0))
        self.send_button.clicked.connect(self.send_motion)
        self.read_button.clicked.connect(self.poll_status)

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
        if self.service.is_connected:
            self.service.disconnect()
            self.poll_timer.stop()
            self._set_connected(False)
            self._log("串口已断开")
            return

        self._reset_service_for_protocol()

        port = self.port_combo.currentData()
        if not port:
            self._show_error("请先选择可用串口")
            return

        try:
            baudrate = self._selected_baudrate()
            stopbits = self._selected_stopbits()
        except ValueError:
            self._show_error("波特率或停止位格式无效")
            return

        try:
            self.service.connect(
                port=port,
                baudrate=baudrate,
                slave_id=self.slave_id_spin.value(),
                timeout=self.timeout_spin.value(),
                stopbits=stopbits,
                retries=self.retries_spin.value(),
            )
            self._poll_failure_count = 0
            self._set_connected(True)
            self._log(
                self._connection_log(port, baudrate, stopbits)
            )
            if self.auto_poll_checkbox.isChecked() and self._selected_protocol() == "modbus":
                self.poll_timer.start()
                self.poll_status()
            elif self._selected_protocol() in {"custom", "can", "canfd"}:
                self._log(f"{self._protocol_label()} 不支持独立轮询；设备在收到控制帧后会立即返回一帧状态。")
        except ModbusServiceError as exc:
            self._show_error(str(exc))
            self._sync_disconnected_state()

    def send_motion(self) -> None:
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
        self.poll_timer.stop()
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
                    "SocketCAN 接口需先由系统配置并启用。"
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
        self.connect_button.setText("断开" if connected else "连接")
        self.send_button.setEnabled(connected)
        self.read_button.setEnabled(connected and self._selected_protocol() == "modbus")
        self.open_button.setEnabled(connected)
        self.close_button.setEnabled(connected)
        self._update_protocol_controls()

    def _update_protocol_controls(self) -> None:
        is_can = self._selected_protocol() in {"can", "canfd"}
        connected = self.service.is_connected
        self.baudrate_combo.setEnabled(not is_can and not connected)
        self.stopbits_combo.setEnabled(not is_can and not connected)
        self.auto_poll_checkbox.setEnabled(self._selected_protocol() == "modbus" and not connected)
        self.scan_button.setEnabled(not is_can and not connected)
        self.full_scan_button.setEnabled(not is_can and not connected)

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
