from __future__ import annotations

import os
import json
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Run actual Qt event delivery and QThreads without a desktop or CAN hardware.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

from omnipicker_tool.can_service import CanMotionResult, OmniPickerCanService
from omnipicker_tool.main_window import MainWindow, parse_can_ids
from omnipicker_tool.protocol_service import DeviceStatus, ProtocolServiceError


def status(position=126):
    return DeviceStatus(None, 0, 1, position, 64, 32)


class FakeCanService(OmniPickerCanService):
    """A fake transport that leaves the window's worker lifecycle real."""

    def __init__(self):
        super().__init__()
        self.connected = False
        self.connect_calls = []
        self.motion_calls = []
        self.command_calls = []
        self.disconnect_calls = 0
        self.connect_threads = []
        self.motion_threads = []
        self.connect_gate = None
        self.motion_gate = None
        self.connect_error = None
        self.motion_error = None
        self.motion_result = None

    @property
    def is_connected(self):
        return self.connected

    @staticmethod
    def _wait(gate):
        if gate is not None and not gate.wait(timeout=5):
            raise RuntimeError("Test did not release background operation")

    def connect(self, **kwargs):
        self.connect_calls.append(kwargs)
        self.connect_threads.append(threading.get_ident())
        self._wait(self.connect_gate)
        if self.connect_error:
            raise self.connect_error
        self._node_id = kwargs["slave_id"]
        self.connected = True

    def disconnect(self):
        self.disconnect_calls += 1
        self.connected = False

    def write_motion_many(self, **kwargs):
        self.motion_calls.append(kwargs)
        self.motion_threads.append(threading.get_ident())
        self._wait(self.motion_gate)
        if self.motion_error:
            raise self.motion_error
        if self.motion_result is not None:
            return self.motion_result
        return CanMotionResult(
            statuses={node_id: status(kwargs["position"]) for node_id in kwargs["node_ids"]},
            errors={},
        )

    def write_motion_commands(self, commands):
        self.command_calls.append(commands)
        self.motion_threads.append(threading.get_ident())
        self._wait(self.motion_gate)
        if self.motion_error:
            raise self.motion_error
        if self.motion_result is not None:
            return self.motion_result
        return CanMotionResult(statuses={node_id: status(values[0]) for node_id, values in commands.items()}, errors={})


class CanIdParsingTests(unittest.TestCase):
    def test_decimal_hex_unicode_separators_and_stable_deduplication(self):
        self.assertEqual(parse_can_ids("2, 0x10；2，001; 0X010\n0 2047\t0x7ff"), [2, 16, 1, 0, 2047])

    def test_empty_or_invalid_ids_are_rejected_before_transmission(self):
        for text in ("", " ,；\n", "-1", "+1", "2048", "0x800", "1.5", "1-3", "abc", "0x", "１２"):
            with self.subTest(text=text), self.assertRaises(ProtocolServiceError):
                parse_can_ids(text)


class MainWindowCanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.interfaces = patch.object(MainWindow, "_socketcan_interfaces", return_value=["can-test"])
        self.interfaces.start()
        self.addCleanup(self.interfaces.stop)
        self.window = MainWindow()
        self.service = FakeCanService()
        self.window.service = self.service
        self.error_dialog = patch.object(self.window, "_show_error")
        self.show_error = self.error_dialog.start()
        self.addCleanup(self.error_dialog.stop)
        self.gates = []

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        self.wait_until(lambda: self.window._can_worker is None)
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def gate(self):
        gate = threading.Event()
        self.gates.append(gate)
        return gate

    def wait_until(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.app.processEvents()
            if time.monotonic() >= deadline:
                self.fail("Timed out waiting for Qt background operation")
            time.sleep(0.002)
        self.app.processEvents()

    def wait_idle(self):
        self.wait_until(lambda: self.window._can_worker is None)

    def connect(self):
        self.window.toggle_connection()
        self.wait_idle()
        self.assertTrue(self.service.is_connected)

    def test_connection_runs_in_worker_and_keeps_qt_event_loop_responsive(self):
        self.service.connect_gate = gate = self.gate()
        self.window.toggle_connection()
        self.wait_until(lambda: bool(self.service.connect_calls))
        self.assertNotEqual(self.service.connect_threads[0], threading.get_ident())
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(True))
        self.wait_until(lambda: bool(ticks))
        self.assertFalse(self.window.connect_button.isEnabled())
        self.assertFalse(self.window.protocol_combo.isEnabled())
        self.assertFalse(self.window.slave_id_spin.isEnabled())
        self.assertEqual(self.service.motion_calls, [])
        gate.set()
        self.wait_idle()
        self.assertEqual(self.service.connect_calls[0]["port"], "can-test")
        self.assertEqual(self.window.connect_button.text(), "断开")
        self.assertTrue(self.window.send_button.isEnabled())
        self.assertTrue(self.window.slave_id_spin.isEnabled())
        self.assertFalse(self.window.poll_timer.isActive())
        self.assertFalse(self.window.read_button.isEnabled())

    def test_connection_error_restores_editable_disconnected_state(self):
        self.service.connect_error = ProtocolServiceError("系统授权已取消")
        self.window.toggle_connection()
        self.wait_idle()
        self.show_error.assert_called_once_with("系统授权已取消")
        self.assertFalse(self.service.is_connected)
        self.assertEqual(self.window.connect_button.text(), "连接")
        self.assertTrue(self.window.connect_button.isEnabled())
        self.assertTrue(self.window.port_combo.isEnabled())
        self.assertFalse(self.window.send_button.isEnabled())

    def test_switch_target_after_connection_sends_to_new_id_without_reconnect(self):
        self.connect()
        self.window._update_status(status())
        self.window.slave_id_spin.setValue(16)
        self.assertEqual(self.service._node_id, 16)
        self.assertEqual(self.window.position_label.text(), "-")
        self.assertEqual(self.window.status_id_label.text(), "-")
        self.window.send_button.click()
        self.wait_idle()
        self.assertEqual(self.service.motion_calls[0]["node_ids"], [16])
        self.assertEqual(len(self.service.connect_calls), 1)
        self.assertEqual(self.service.disconnect_calls, 0)
        self.assertEqual(self.window.status_id_label.text(), "16")

    def test_multitarget_open_and_close_send_deduplicated_targets(self):
        self.connect()
        self.window.multi_id_checkbox.setChecked(True)
        self.assertTrue(self.window.can_ids_edit.isEnabled())
        self.window.can_ids_edit.setText("1, 2; 0x10, 2")
        self.window.open_button.click()
        self.wait_idle()
        self.window.close_button.click()
        self.wait_idle()
        self.assertEqual([call["node_ids"] for call in self.service.motion_calls], [[1, 2, 16], [1, 2, 16]])
        self.assertEqual([call["position"] for call in self.service.motion_calls], [255, 0])
        for call in self.service.motion_calls:
            self.assertEqual((call["speed"], call["force"], call["acceleration"], call["deceleration"]), (255, 255, 255, 255))

    def test_unchecked_multitarget_field_does_not_change_single_target(self):
        self.connect()
        self.window.can_ids_edit.setText("5,6")
        self.window.slave_id_spin.setValue(3)
        self.window.send_button.click()
        self.wait_idle()
        self.assertEqual(self.service.motion_calls[0]["node_ids"], [3])

    def test_invalid_multitarget_list_does_not_send_any_commands(self):
        self.connect()
        self.window.multi_id_checkbox.setChecked(True)
        self.window.can_ids_edit.setText("1, 2048")
        self.window.send_button.click()
        self.assertEqual(self.service.motion_calls, [])
        self.assertIsNone(self.window._can_worker)
        self.show_error.assert_called_once()
        self.assertTrue(self.window.send_button.isEnabled())

    def test_partial_batch_feedback_is_mapped_to_each_id(self):
        self.connect()
        self.window.multi_id_checkbox.setChecked(True)
        self.window.can_ids_edit.setText("2, 1, 16")
        self.service.motion_result = CanMotionResult(statuses={1: status(42), 16: status(99)}, errors={2: "等待 CAN ID 2 回执超时"})
        self.window.send_button.click()
        self.wait_idle()
        table = self.window.can_status_table
        self.assertEqual(table.rowCount(), 3)
        self.assertEqual(table.item(0, 0).text(), "2 (0x2)")
        self.assertIn("失败", table.item(0, 1).text())
        self.assertIn("超时", table.item(0, 1).toolTip())
        self.assertEqual(table.item(0, 4).text(), "-")
        self.assertEqual(table.item(1, 1).text(), "已收到")
        self.assertEqual(table.item(1, 4).text(), "42")
        self.assertEqual(table.item(2, 4).text(), "99")
        self.assertEqual(self.window.status_id_label.text(), "1")
        self.assertTrue(self.window.position_label.text().startswith("42 ("))
        self.assertIn("2/3", self.window.log_view.toPlainText())
        self.assertTrue(self.window.send_button.isEnabled())
        self.show_error.assert_not_called()

    def test_failed_selected_id_does_not_retain_other_id_status(self):
        self.connect()
        self.window._update_status(status(100))
        self.window.multi_id_checkbox.setChecked(True)
        self.window.can_ids_edit.setText("1,2")
        self.service.motion_result = CanMotionResult(statuses={2: status(50)}, errors={1: "超时"})
        self.window.send_button.click()
        self.wait_idle()
        self.assertEqual(self.window.position_label.text(), "-")
        self.assertEqual(self.window.status_id_label.text(), "-")

    def test_pending_motion_disables_editors_and_rejects_overlapping_actions(self):
        self.connect()
        self.window.multi_id_checkbox.setChecked(True)
        self.window.can_ids_edit.setText("1,2")
        self.service.motion_gate = gate = self.gate()
        self.window.send_button.click()
        self.wait_until(lambda: len(self.service.motion_calls) == 1)
        self.assertNotEqual(self.service.motion_threads[0], threading.get_ident())
        for widget in (self.window.send_button, self.window.open_button, self.window.close_button,
                       self.window.connect_button, self.window.slave_id_spin, self.window.multi_id_checkbox,
                       self.window.can_ids_edit, self.window.protocol_combo, self.window.position_editor):
            self.assertFalse(widget.isEnabled())
        self.assertEqual(self.window.can_status_table.item(0, 1).text(), "等待回执")
        self.window.send_motion()
        self.window.toggle_connection()
        self.assertEqual(len(self.service.motion_calls), 1)
        self.assertEqual(len(self.service.connect_calls), 1)
        self.assertEqual(self.service.disconnect_calls, 0)
        gate.set()
        self.wait_idle()
        self.assertTrue(self.window.send_button.isEnabled())
        self.assertTrue(self.window.can_ids_edit.isEnabled())

    def test_motion_exception_marks_pending_rows_failed_and_releases_controls(self):
        self.connect()
        self.window.multi_id_checkbox.setChecked(True)
        self.window.can_ids_edit.setText("1,2")
        self.service.motion_error = ProtocolServiceError("发送失败")
        self.window.send_button.click()
        self.wait_idle()
        self.show_error.assert_called_once_with("发送失败")
        self.assertTrue(self.window.send_button.isEnabled())
        for row in range(2):
            self.assertIn("失败", self.window.can_status_table.item(row, 1).text())

    def test_close_waits_for_motion_before_disconnecting_service(self):
        self.connect()
        self.window.show()
        self.app.processEvents()
        self.service.motion_gate = gate = self.gate()
        self.window.send_button.click()
        self.wait_until(lambda: bool(self.service.motion_calls))
        self.window.close()
        self.assertTrue(self.window.isVisible())
        self.assertTrue(self.window._close_when_idle)
        self.assertEqual(self.service.disconnect_calls, 0)
        gate.set()
        self.wait_idle()
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.service.is_connected)
        self.assertEqual(self.service.disconnect_calls, 1)

    def test_close_during_authorization_error_suppresses_modal_dialog(self):
        self.window.show()
        self.service.connect_gate = gate = self.gate()
        self.service.connect_error = ProtocolServiceError("授权已取消")
        self.window.toggle_connection()
        self.wait_until(lambda: bool(self.service.connect_calls))
        self.window.close()
        self.assertTrue(self.window.isVisible())
        gate.set()
        self.wait_idle()
        self.assertFalse(self.window.isVisible())
        self.show_error.assert_not_called()

    def test_device_cards_send_independent_parameters_to_selected_devices(self):
        self.connect()
        first, second = self.window.device_cards
        third = self.window.add_device_card(16, "第三个夹爪")
        first_values = (10, 20, 30, 40, 50)
        third_values = (60, 70, 80, 90, 100)
        for card, values in ((first, first_values), (third, third_values)):
            for spin, value in zip(card.parameters.values(), values):
                spin.setValue(value)
        second.selected_checkbox.setChecked(False)
        self.window.cards_send_button.click()
        self.wait_idle()
        self.assertEqual(self.service.command_calls, [{1: first_values, 16: third_values}])
        self.assertEqual(self.service.motion_calls, [])
        self.assertIn("已收到", first.receipt_label.text())
        self.assertIn("位置 10", first.feedback_label.text())
        self.assertIn("位置 60", third.feedback_label.text())
        self.assertEqual(second.receipt_label.text(), "未发送")

    def test_card_quick_actions_are_independent_of_batch_selection(self):
        self.connect()
        first, second = self.window.device_cards
        first.selected_checkbox.setChecked(False)
        first.parameters["speed"].setValue(77)
        first.open_button.click()
        self.wait_idle()
        first.close_button.click()
        self.wait_idle()
        self.assertEqual(self.service.command_calls, [{1: (255, 77, 255, 255, 255)}, {1: (0, 77, 255, 255, 255)}])
        self.assertEqual(second.parameters["position"].value(), 127)
        self.assertEqual(second.receipt_label.text(), "未发送")

    def test_card_batch_open_close_preserves_each_devices_other_parameters(self):
        self.connect()
        first, second = self.window.device_cards
        first.parameters["force"].setValue(20)
        second.parameters["speed"].setValue(80)
        self.window.cards_open_button.click()
        self.wait_idle()
        self.window.cards_close_button.click()
        self.wait_idle()
        self.assertEqual(self.service.command_calls, [
            {1: (255, 255, 20, 255, 255), 2: (255, 80, 255, 255, 255)},
            {1: (0, 255, 20, 255, 255), 2: (0, 80, 255, 255, 255)},
        ])

    def test_duplicate_card_ids_and_empty_selection_send_nothing(self):
        self.connect()
        first, second = self.window.device_cards
        second.id_spin.setValue(first.node_id)
        self.window.cards_send_button.click()
        self.assertEqual(self.service.command_calls, [])
        self.assertIn("重复", self.show_error.call_args.args[0])
        first.selected_checkbox.setChecked(False)
        second.selected_checkbox.setChecked(False)
        self.window.cards_send_button.click()
        self.assertEqual(self.service.command_calls, [])
        self.assertIn("勾选", self.show_error.call_args.args[0])

    def test_card_feedback_partial_timeout_and_id_change_clear_only_that_card(self):
        self.connect()
        first, second = self.window.device_cards
        self.service.motion_result = CanMotionResult(statuses={1: status(88)}, errors={2: "等待 CAN ID 2 回执超时"})
        self.window.cards_send_button.click()
        self.wait_idle()
        self.assertIn("ID 1", first.receipt_label.text())
        self.assertIn("位置 88", first.feedback_label.text())
        self.assertIn("超时", second.receipt_label.text())
        self.assertEqual(second.feedback_label.text(), "位置 -  ·  速度 -  ·  力矩 -")
        first.id_spin.setValue(42)
        self.assertEqual(first.receipt_label.text(), "未发送")
        self.assertEqual(first.feedback_label.text(), "位置 -  ·  速度 -  ·  力矩 -")
        self.assertIn("超时", second.receipt_label.text())
        self.assertEqual(len(self.service.connect_calls), 1)
        self.assertEqual(self.service.disconnect_calls, 0)

    def test_pending_card_operation_disables_card_mutation_and_parallel_sends(self):
        self.connect()
        first, second = self.window.device_cards
        self.service.motion_gate = gate = self.gate()
        first.send_button.click()
        self.wait_until(lambda: bool(self.service.command_calls))
        for card in (first, second):
            for widget in (card.id_spin, card.name_edit, card.remove_button, card.selected_checkbox,
                           card.send_button, card.open_button, card.close_button, *card.parameters.values()):
                self.assertFalse(widget.isEnabled())
        for widget in (self.window.add_device_button, self.window.load_devices_button,
                       self.window.cards_send_button, self.window.cards_open_button, self.window.cards_close_button):
            self.assertFalse(widget.isEnabled())
        self.window.remove_device_card(first)
        self.assertEqual(self.window.device_cards, [first, second])
        self.assertIsNone(self.window.add_device_card())
        self.window.send_device_cards()
        self.assertEqual(len(self.service.command_calls), 1)
        gate.set()
        self.wait_idle()
        self.assertTrue(first.id_spin.isEnabled())
        self.assertTrue(second.send_button.isEnabled())

    def test_card_motion_exception_replaces_pending_receipts(self):
        self.connect()
        self.service.motion_error = ProtocolServiceError("接口已关闭")
        self.window.cards_send_button.click()
        self.wait_idle()
        for card in self.window.device_cards:
            self.assertIn("接口已关闭", card.receipt_label.text())
            self.assertNotIn("等待回执", card.receipt_label.text())

    def test_device_configuration_round_trip_does_not_send_motion(self):
        first, second = self.window.device_cards
        first.name_edit.setText("左侧夹爪")
        first.id_spin.setValue(16)
        first.parameters["position"].setValue(10)
        first.parameters["speed"].setValue(25)
        second.selected_checkbox.setChecked(False)
        expected = [card.configuration() for card in self.window.device_cards]
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "devices.json")
            with patch("omnipicker_tool.main_window.QFileDialog.getSaveFileName", return_value=(path, "")):
                self.window.save_device_config()
            saved = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(saved, {"version": 1, "devices": expected})
            self.window.remove_device_card(first)
            second.id_spin.setValue(40)
            with patch("omnipicker_tool.main_window.QFileDialog.getOpenFileName", return_value=(path, "")):
                self.window.load_device_config()
        self.assertEqual([card.configuration() for card in self.window.device_cards], expected)
        self.assertEqual(self.service.command_calls, [])
        self.assertEqual(self.service.motion_calls, [])
        self.show_error.assert_not_called()

    def test_invalid_device_configurations_leave_existing_cards_unchanged(self):
        originals = list(self.window.device_cards)
        original_configs = [card.configuration() for card in originals]
        invalid = [
            "{malformed json", "[]", '{"version": 2, "devices": []}',
            json.dumps({"version": 1, "devices": [original_configs[0], original_configs[0]]}),
        ]
        for field, value in (("id", 2048), ("id", True), ("selected", "yes"), ("name", 1), ("parameters", {})):
            bad_device = dict(original_configs[0], **{field: value})
            invalid.append(json.dumps({"version": 1, "devices": [bad_device]}))
        bad_parameters = dict(original_configs[0], parameters=dict(original_configs[0]["parameters"], force=256))
        invalid.append(json.dumps({"version": 1, "devices": [bad_parameters]}))
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "invalid.json")
            for content in invalid:
                with self.subTest(content=content):
                    Path(path).write_text(content, encoding="utf-8")
                    self.show_error.reset_mock()
                    with patch("omnipicker_tool.main_window.QFileDialog.getOpenFileName", return_value=(path, "")):
                        self.window.load_device_config()
                    self.assertEqual(self.window.device_cards, originals)
                    self.assertEqual([card.configuration() for card in self.window.device_cards], original_configs)
                    self.show_error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
