from __future__ import annotations

import unittest

from omnipicker_tool.custom_service import OmniPickerCustomService
from omnipicker_tool.protocol_service import ProtocolServiceError


class FakeSerial:
    def __init__(self, reply_factory, max_read_size=None):
        self.is_open = True
        self.timeout = 0.05
        self._reply_factory = reply_factory
        self._max_read_size = max_read_size
        self._buffer = bytearray()
        self.written = b""

    @property
    def in_waiting(self):
        if self._max_read_size is None:
            return len(self._buffer)
        return min(len(self._buffer), self._max_read_size)

    def reset_input_buffer(self):
        self._buffer.clear()

    def write(self, data):
        self.written = bytes(data)
        self._buffer.extend(self._reply_factory(self.written))
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.is_open = False

    def read(self, size):
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data


def status_frame():
    body = [1, 0, 1, 126, 64, 32, 0, 0, 0]
    return b"AA" + bytes(body) + bytes([OmniPickerCustomService._checksum(body)])


class CustomServiceTests(unittest.TestCase):
    def make_service(self, reply_factory, max_read_size=None):
        service = OmniPickerCustomService()
        service._serial = FakeSerial(reply_factory, max_read_size)
        return service

    def test_official_command_frame_and_status(self):
        service = self.make_service(lambda _request: status_frame())
        status = service.write_motion(127, 255, 255, 255, 255)
        self.assertEqual(service._serial.written, bytes.fromhex("41 41 01 00 7f ff ff ff ff 00 00 83"))
        self.assertEqual((status.position, status.speed, status.force), (126, 64, 32))

    def test_ignores_adapter_echo_before_status(self):
        service = self.make_service(lambda request: request + status_frame())
        self.assertEqual(service.write_motion(127, 255, 255, 255, 255).position, 126)

    def test_resynchronizes_after_leading_noise(self):
        service = self.make_service(lambda _request: b"\x00\xffnoise" + status_frame())
        self.assertEqual(service.write_motion(127, 255, 255, 255, 255).force, 32)

    def test_accepts_response_split_across_usb_reads(self):
        service = self.make_service(lambda _request: status_frame(), max_read_size=3)
        self.assertEqual(service.write_motion(127, 255, 255, 255, 255).position, 126)

    def test_eio_disconnects_without_leaking_exception(self):
        service = self.make_service(lambda _request: status_frame())

        def fail_reset():
            raise RuntimeError("模拟 termios EIO")

        service._serial.reset_input_buffer = fail_reset
        with self.assertRaisesRegex(ProtocolServiceError, "串口已断开"):
            service.write_motion(127, 255, 255, 255, 255)
        self.assertFalse(service.is_connected)


if __name__ == "__main__":
    unittest.main()
