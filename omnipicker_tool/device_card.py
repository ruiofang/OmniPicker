from __future__ import annotations

from datetime import datetime

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QCheckBox, QGridLayout, QGroupBox
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox

from omnipicker_tool.constants import FAULT_CODE_LABELS, STATE_LABELS
from omnipicker_tool.protocol_service import DeviceStatus


class DeviceCard(QGroupBox):
    motion_requested = pyqtSignal(object)
    remove_requested = pyqtSignal(object)

    def __init__(self, node_id: int, name: str = "") -> None:
        super().__init__("CAN 设备")
        self.selected_checkbox = QCheckBox("加入批量控制")
        self.selected_checkbox.setChecked(True)
        self.name_edit = QLineEdit(name or f"夹爪 {node_id}")
        self.id_spin = QSpinBox()
        self.id_spin.setRange(0, 0x7FF)
        self.id_spin.setValue(node_id)
        self.id_spin.setToolTip("填写设备已配置的 CAN ID；修改后下一次发送生效。")
        self.remove_button = QPushButton("移除")
        self.open_button = QPushButton("全开")
        self.close_button = QPushButton("全闭")
        self.send_button = QPushButton("发送运动")

        layout = QGridLayout(self)
        header = QHBoxLayout()
        header.addWidget(self.selected_checkbox)
        header.addStretch()
        header.addWidget(self.remove_button)
        layout.addLayout(header, 0, 0, 1, 4)
        layout.addWidget(QLabel("设备名称"), 1, 0)
        layout.addWidget(self.name_edit, 1, 1)
        layout.addWidget(QLabel("CAN ID"), 1, 2)
        layout.addWidget(self.id_spin, 1, 3)
        self.parameters: dict[str, QSpinBox] = {}
        for index, (key, label, initial) in enumerate([
            ("position", "目标位置", 127), ("speed", "目标速度", 255),
            ("force", "目标力矩", 255), ("acceleration", "目标加速度", 255),
            ("deceleration", "目标减速度", 255),
        ]):
            spin = QSpinBox()
            spin.setRange(0, 255)
            spin.setValue(initial)
            self.parameters[key] = spin
            row, column = 2 + index // 2, (index % 2) * 2
            layout.addWidget(QLabel(label), row, column)
            layout.addWidget(spin, row, column + 1)
        actions = QHBoxLayout()
        for button in (self.close_button, self.open_button, self.send_button):
            actions.addWidget(button)
        layout.addLayout(actions, 5, 0, 1, 4)
        self.receipt_label = QLabel("未发送")
        self.receipt_label.setWordWrap(True)
        self.feedback_label = QLabel("位置 -  ·  速度 -  ·  力矩 -")
        self.feedback_label.setWordWrap(True)
        self.state_label = QLabel("状态 -  ·  错误码 -")
        self.state_label.setWordWrap(True)
        self.updated_label = QLabel("刷新时间 -")
        layout.addWidget(self.receipt_label, 6, 0, 1, 4)
        layout.addWidget(self.feedback_label, 7, 0, 1, 4)
        layout.addWidget(self.state_label, 8, 0, 1, 4)
        layout.addWidget(self.updated_label, 9, 0, 1, 4)
        self.id_spin.valueChanged.connect(self.clear_status)
        self.send_button.clicked.connect(lambda: self.request_motion())
        self.open_button.clicked.connect(lambda: self.request_motion(255))
        self.close_button.clicked.connect(lambda: self.request_motion(0))
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self))
        self.set_controls(False, False)

    @property
    def node_id(self) -> int:
        return self.id_spin.value()

    def motion_values(self) -> tuple[int, int, int, int, int]:
        return tuple(spin.value() for spin in self.parameters.values())

    def request_motion(self, position: int | None = None) -> None:
        if position is not None:
            self.parameters["position"].setValue(position)
        self.motion_requested.emit(self)

    def set_controls(self, connected: bool, busy: bool) -> None:
        for widget in (self.name_edit, self.id_spin, self.selected_checkbox, self.remove_button,
                       *self.parameters.values()):
            widget.setEnabled(not busy)
        for button in (self.open_button, self.close_button, self.send_button):
            button.setEnabled(connected and not busy)

    def clear_status(self) -> None:
        self.receipt_label.setText("未发送")
        self.receipt_label.setToolTip("")
        self.feedback_label.setText("位置 -  ·  速度 -  ·  力矩 -")
        self.state_label.setText("状态 -  ·  错误码 -")
        self.updated_label.setText("刷新时间 -")

    def mark_pending(self) -> None:
        self.clear_status()
        self.receipt_label.setText(f"ID {self.node_id} (0x{self.node_id:X}) · 等待回执…")

    def show_status(self, status: DeviceStatus) -> None:
        self.receipt_label.setText(f"ID {self.node_id} (0x{self.node_id:X}) · 已收到回执")
        self.feedback_label.setText(f"位置 {status.position}  ·  速度 {status.speed}  ·  力矩 {status.force}")
        self.state_label.setText(
            f"状态 {STATE_LABELS.get(status.state, '未知')} ({status.state})  ·  "
            f"错误码 {FAULT_CODE_LABELS.get(status.fault_code, '未知')} (0x{status.fault_code:02X})"
        )
        self.updated_label.setText(f"刷新时间 {datetime.now():%H:%M:%S}")

    def show_error(self, error: str) -> None:
        self.clear_status()
        self.receipt_label.setText(f"ID {self.node_id} · {error[:160]}")
        self.receipt_label.setToolTip(error)
        self.updated_label.setText(f"刷新时间 {datetime.now():%H:%M:%S}")

    def configuration(self) -> dict:
        return {"name": self.name_edit.text(), "id": self.node_id,
                "selected": self.selected_checkbox.isChecked(),
                "parameters": {key: spin.value() for key, spin in self.parameters.items()}}
