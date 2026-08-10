from __future__ import annotations

import unittest

from omnipicker_tool.can_service import OmniPickerCanService


class FakeMessage:
    def __init__(self, arbitration_id, data, is_extended_id=False, is_fd=False, bitrate_switch=False):
        self.arbitration_id = arbitration_id
        self.data = data
        self.is_extended_id = is_extended_id
        self.is_fd = is_fd
        self.bitrate_switch = bitrate_switch


class FakeCan:
    Message = FakeMessage


class FakeBus:
    def __init__(self):
        self.sent = None
        self.responses = [FakeMessage(1, bytes([0, 1, 126, 64, 32, 0, 0, 0]))]

    def send(self, message, timeout):
        del timeout
        self.sent = message

    def recv(self, timeout):
        del timeout
        return self.responses.pop(0) if self.responses else None

    def shutdown(self):
        pass


class CanServiceTests(unittest.TestCase):
    def make_service(self, is_fd=False):
        service = OmniPickerCanService(is_fd=is_fd)
        bus = FakeBus()
        service._bus = bus
        service._can = FakeCan()
        return service, bus

    def test_classic_can_command_and_status(self):
        service, bus = self.make_service()
        status = service.write_motion(10, 20, 30, 40, 50)
        self.assertEqual(bus.sent.arbitration_id, 1)
        self.assertEqual(bytes(bus.sent.data), bytes.fromhex("00 0a 14 1e 28 32 00 00"))
        self.assertFalse(bus.sent.is_fd)
        self.assertEqual((status.position, status.speed, status.force), (126, 64, 32))

    def test_can_fd_enables_bitrate_switch(self):
        service, bus = self.make_service(is_fd=True)
        service.write_motion(10, 20, 30, 40, 50)
        self.assertTrue(bus.sent.is_fd)
        self.assertTrue(bus.sent.bitrate_switch)


if __name__ == "__main__":
    unittest.main()
