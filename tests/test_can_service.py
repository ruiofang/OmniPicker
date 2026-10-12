from __future__ import annotations

from collections import Counter, deque
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from omnipicker_tool.can_service import OmniPickerCanService
from omnipicker_tool.protocol_service import ProtocolServiceError


MOTION = (10, 20, 30, 40, 50)


class FakeMessage:
    def __init__(
        self, arbitration_id, data, is_extended_id=False, is_fd=False,
        bitrate_switch=False, is_error_frame=False, is_remote_frame=False, is_rx=True,
    ):
        self.arbitration_id = arbitration_id
        self.data = data
        self.is_extended_id = is_extended_id
        self.is_fd = is_fd
        self.bitrate_switch = bitrate_switch
        self.is_error_frame = is_error_frame
        self.is_remote_frame = is_remote_frame
        self.is_rx = is_rx


def status_frame(node_id, position=126, **flags):
    return FakeMessage(node_id, bytes([0, 1, position, 64, 32, 0, 0, 0]), **flags)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class FakeBus:
    """Replies are queued by send, so pre-send draining behaves like a real bus."""

    def __init__(self, clock):
        self.clock = clock
        self.sent = []
        self.events = []
        self.responses = deque()
        self.send_counts = Counter()
        self.closed = False
        self.channel_info = "socketcan channel 'can3'"
        self.on_send = lambda message, count: [status_frame(message.arbitration_id)]

    def send(self, message, timeout):
        self.events.append(("send", message.arbitration_id))
        self.sent.append(message)
        self.send_counts[message.arbitration_id] += 1
        self.responses.extend(self.on_send(message, self.send_counts[message.arbitration_id]))

    def recv(self, timeout):
        self.events.append(("recv", timeout))
        if self.responses:
            response = self.responses.popleft()
            if isinstance(response, Exception):
                raise response
            return response
        self.clock.now += timeout
        return None

    def shutdown(self):
        self.closed = True


class CanServiceTests(unittest.TestCase):
    def make_service(self, is_fd=False):
        service = OmniPickerCanService(is_fd=is_fd)
        clock = FakeClock()
        bus = FakeBus(clock)
        service._bus = bus
        service._can = SimpleNamespace(Message=FakeMessage)
        clock_patch = patch("omnipicker_tool.can_service.monotonic", clock.monotonic)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        return service, bus

    def test_classic_can_command_and_status(self):
        service, bus = self.make_service()
        status = service.write_motion(*MOTION)
        self.assertEqual(bus.sent[0].arbitration_id, 1)
        self.assertEqual(bytes(bus.sent[0].data), bytes.fromhex("00 0a 14 1e 28 32 00 00"))
        self.assertFalse(bus.sent[0].is_fd)
        self.assertEqual((status.position, status.speed, status.force), (126, 64, 32))

    def test_can_fd_enables_bitrate_switch(self):
        service, bus = self.make_service(is_fd=True)
        service.write_motion(*MOTION)
        self.assertTrue(bus.sent[0].is_fd)
        self.assertTrue(bus.sent[0].bitrate_switch)

    def test_can_change_target_while_connected(self):
        service, bus = self.make_service()
        service.write_motion(*MOTION)
        service.set_node_id(0x7FF)
        service.write_motion(*MOTION)
        self.assertEqual([message.arbitration_id for message in bus.sent], [1, 0x7FF])
        self.assertTrue(service.is_connected)
        self.assertFalse(bus.closed)

    def test_all_targets_are_sent_before_waiting_for_any_response(self):
        service, bus = self.make_service()
        result = service.write_motion_many([1, 2, 3], *MOTION)
        self.assertEqual(set(result.statuses), {1, 2, 3})
        self.assertEqual(result.errors, {})
        self.assertEqual(bus.events[:5], [
            ("recv", 0), ("send", 1), ("send", 2), ("send", 3), ("recv", service._timeout),
        ])

    def test_each_card_can_send_different_motion_parameters(self):
        service, bus = self.make_service()
        commands = {1: MOTION, 7: (200, 210, 220, 230, 240)}

        def reply(message, _count):
            # Drivers/adapters may reflect commands; filter each ID's payload.
            return [message, status_frame(message.arbitration_id, message.data[1])]

        bus.on_send = reply
        result = service.write_motion_commands(commands)
        self.assertEqual(bytes(bus.sent[0].data), bytes.fromhex("00 0a 14 1e 28 32 00 00"))
        self.assertEqual(bytes(bus.sent[1].data), bytes.fromhex("00 c8 d2 dc e6 f0 00 00"))
        self.assertEqual({node_id: status.position for node_id, status in result.statuses.items()},
                         {1: 10, 7: 200})
        self.assertEqual(result.errors, {})

    def test_missing_first_target_does_not_hide_other_nodes(self):
        service, bus = self.make_service()
        bus.on_send = lambda message, count: (
            [] if message.arbitration_id == 1 else [status_frame(message.arbitration_id)]
        )
        result = service.write_motion_many([1, 2, 3], *MOTION)
        self.assertEqual(set(result.statuses), {2, 3})
        self.assertEqual(set(result.errors), {1})
        self.assertAlmostEqual(bus.clock.now, service._timeout)
        self.assertTrue(service.is_connected)

    def test_out_of_order_responses_are_matched_by_id(self):
        service, bus = self.make_service()
        bus.on_send = lambda message, count: (
            [status_frame(3, 33), status_frame(1, 11), status_frame(2, 22)]
            if message.arbitration_id == 3 else []
        )
        result = service.write_motion_many([1, 2, 3], *MOTION)
        self.assertEqual({node_id: status.position for node_id, status in result.statuses.items()},
                         {1: 11, 2: 22, 3: 33})

    def test_retries_only_missing_nodes(self):
        service, bus = self.make_service()
        service._retries = 1
        bus.on_send = lambda message, count: (
            [] if message.arbitration_id == 2 and count == 1
            else [status_frame(message.arbitration_id)]
        )
        result = service.write_motion_many([1, 2], *MOTION)
        self.assertEqual(bus.send_counts, {1: 1, 2: 2})
        self.assertEqual(set(result.statuses), {1, 2})
        self.assertEqual(result.errors, {})
        self.assertEqual(sum(event == ("recv", 0) for event in bus.events), 1)

    def test_unresponsive_batch_uses_one_timeout_per_attempt(self):
        service, bus = self.make_service()
        service._retries = 2
        bus.on_send = lambda message, count: []
        result = service.write_motion_many([1, 2, 3], *MOTION)
        self.assertEqual(set(result.errors), {1, 2, 3})
        self.assertEqual(result.statuses, {})
        self.assertEqual(bus.send_counts, {1: 3, 2: 3, 3: 3})
        self.assertAlmostEqual(bus.clock.now, service._timeout * 3)

    def test_queued_old_status_is_not_used_for_a_new_command(self):
        service, bus = self.make_service()
        bus.responses.append(status_frame(1, 77))
        status = service.write_motion(*MOTION)
        self.assertEqual(status.position, 126)

    def test_invalid_frames_cannot_satisfy_status_wait(self):
        service, bus = self.make_service()
        bus.on_send = lambda message, count: [
            status_frame(1, 10, is_extended_id=True),
            status_frame(1, 11, is_error_frame=True),
            status_frame(1, 12, is_remote_frame=True),
            status_frame(1, 13, is_rx=False),
            status_frame(2, 14),
            FakeMessage(1, b"short"),
            FakeMessage(1, message.data),
            status_frame(1, 99),
        ]
        self.assertEqual(service.write_motion(*MOTION).position, 99)

    def test_received_status_survives_subsequent_receive_failure(self):
        service, bus = self.make_service()
        bus.on_send = lambda message, count: (
            [status_frame(1), OSError("bus receive failed")] if message.arbitration_id == 2 else []
        )
        result = service.write_motion_many([1, 2], *MOTION)
        self.assertEqual(set(result.statuses), {1})
        self.assertEqual(set(result.errors), {2})
        self.assertIn("bus receive failed", result.errors[2])
        self.assertFalse(service.is_connected)
        self.assertTrue(bus.closed)

    def test_received_status_survives_send_failure_on_retry(self):
        service, bus = self.make_service()
        service._retries = 2

        def reply(message, count):
            if message.arbitration_id == 1:
                return [status_frame(1)]
            if count == 2:
                raise OSError(100, "Network is down")
            return []

        bus.on_send = reply
        result = service.write_motion_many([1, 2, 3], *MOTION)
        self.assertEqual(set(result.statuses), {1})
        self.assertEqual(set(result.errors), {2, 3})
        self.assertIn("can3", result.errors[2])
        self.assertEqual(bus.send_counts, {1: 1, 2: 2, 3: 1})
        self.assertFalse(service.is_connected)

    def test_failure_while_draining_does_not_transmit(self):
        service, bus = self.make_service()
        bus.responses.append(OSError("read failed"))
        result = service.write_motion_many([1, 2], *MOTION)
        self.assertEqual(set(result.errors), {1, 2})
        self.assertEqual(bus.sent, [])
        self.assertFalse(service.is_connected)

    def test_single_target_timeout_keeps_exception_api(self):
        service, bus = self.make_service()
        bus.on_send = lambda message, count: []
        with self.assertRaisesRegex(ProtocolServiceError, "ID=0x1"):
            service.write_motion(*MOTION)

    def test_deduplicated_batch_does_not_change_single_target(self):
        service, bus = self.make_service()
        service.set_node_id(4)
        result = service.write_motion_many([0, 0, 0x7FF], *MOTION)
        self.assertEqual(set(result.statuses), {0, 0x7FF})
        service.write_motion(*MOTION)
        self.assertEqual([message.arbitration_id for message in bus.sent], [0, 0x7FF, 4])

    def test_invalid_ids_are_rejected_before_sending_anything(self):
        service, bus = self.make_service()
        for invalid in (-1, 0x800, 1.5, "1", True):
            with self.subTest(node_id=invalid):
                with self.assertRaises(ProtocolServiceError):
                    service.set_node_id(invalid)
                with self.assertRaises(ProtocolServiceError):
                    service.write_motion_many([2, invalid], *MOTION)
        self.assertEqual(bus.sent, [])
        self.assertEqual(service._node_id, 1)

    def test_empty_batch_is_rejected(self):
        service, bus = self.make_service()
        with self.assertRaises(ProtocolServiceError):
            service.write_motion_many([], *MOTION)
        self.assertEqual(bus.sent, [])

    def test_all_card_parameters_are_validated_before_transmission(self):
        service, bus = self.make_service()
        with self.assertRaises(ProtocolServiceError):
            service.write_motion_commands({1: MOTION, 2: (1, 2, 3, 4)})
        with self.assertRaises(ProtocolServiceError):
            service.write_motion_commands({1: MOTION, 2: (1, 2, None, 4, 5)})
        self.assertEqual(bus.sent, [])

    def test_disconnected_batch_raises_clear_error(self):
        with self.assertRaisesRegex(ProtocolServiceError, "尚未连接"):
            OmniPickerCanService().write_motion_many([1, 2], *MOTION)

    def test_connect_prepares_interface_before_opening_bus_and_enables_fd(self):
        for is_fd in (False, True):
            with self.subTest(is_fd=is_fd):
                service = OmniPickerCanService(is_fd=is_fd)
                events = []
                bus = Mock()
                module = SimpleNamespace(Bus=Mock(side_effect=lambda **kwargs: events.append("open") or bus))
                with patch.dict("sys.modules", {"can": module}), patch(
                    "omnipicker_tool.can_service.ensure_socketcan_ready",
                    side_effect=lambda *args: events.append("setup"),
                ) as prepare:
                    service.connect("can3", 1000000, 7, 0.2, 1, 2)
                self.assertEqual(events, ["setup", "open"])
                prepare.assert_called_once_with("can3", is_fd)
                module.Bus.assert_called_once_with(
                    interface="socketcan", channel="can3", receive_own_messages=False, fd=is_fd,
                )
                self.assertTrue(service.is_connected)
                self.assertEqual(service._node_id, 7)
                self.assertEqual(service._retries, 2)

    def test_setup_failure_does_not_open_socket(self):
        service = OmniPickerCanService()
        module = SimpleNamespace(Bus=Mock())
        with patch.dict("sys.modules", {"can": module}), patch(
            "omnipicker_tool.can_service.ensure_socketcan_ready",
            side_effect=ProtocolServiceError("authorization cancelled"),
        ):
            with self.assertRaisesRegex(ProtocolServiceError, "authorization cancelled"):
                service.connect("can0", 1000000, 1, 0.3, 1, 0)
        module.Bus.assert_not_called()
        self.assertFalse(service.is_connected)

    def test_connect_rejects_invalid_id_before_interface_setup(self):
        with patch("omnipicker_tool.can_service.ensure_socketcan_ready") as prepare:
            with self.assertRaises(ProtocolServiceError):
                OmniPickerCanService().connect("can0", 1000000, 0x800, 0.3, 1, 0)
        prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
