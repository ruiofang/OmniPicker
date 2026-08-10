from __future__ import annotations

from time import monotonic

from serial import Serial

from omnipicker_tool.protocol_service import DeviceStatus
from omnipicker_tool.protocol_service import ProtocolServiceError


FRAME_HEAD = b"AA"
WRITE_FRAME_LENGTH = 12
READ_FRAME_LENGTH = 12


class OmniPickerCustomService:
    def __init__(self) -> None:
        self._serial: Serial | None = None
        self._slave_id = 1
        self._retries = 0
        self._last_tx = b""
        self._last_rx = b""

    @property
    def is_connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def connect(
        self,
        port: str,
        baudrate: int,
        slave_id: int,
        timeout: float,
        stopbits: float,
        retries: int,
    ) -> None:
        self.disconnect()
        try:
            self._serial = Serial(
                port=port,
                baudrate=baudrate,
                bytesize=8,
                parity="N",
                stopbits=stopbits,
                timeout=timeout,
                write_timeout=timeout,
            )
        except Exception as exc:
            raise ProtocolServiceError(f"串口初始化失败: {exc}") from exc
        self._slave_id = slave_id
        self._retries = max(0, int(retries))
        self._last_tx = b""
        self._last_rx = b""

    def disconnect(self) -> None:
        serial_port = self._serial
        self._serial = None
        if serial_port is not None:
            try:
                serial_port.close()
            except Exception:
                pass

    def probe(self) -> int:
        status = self.write_motion(
            position=127,
            speed=255,
            force=32,
            acceleration=255,
            deceleration=255,
        )
        return status.fault_code

    def write_motion(
        self,
        position: int,
        speed: int,
        force: int,
        acceleration: int,
        deceleration: int,
    ) -> DeviceStatus:
        payload = [
            self._clamp_u8(self._slave_id),
            0,
            self._clamp_u8(position),
            self._clamp_u8(force),
            self._clamp_u8(speed),
            self._clamp_u8(acceleration),
            self._clamp_u8(deceleration),
            0,
            0,
        ]
        frame = bytearray(FRAME_HEAD)
        frame.extend(payload)
        frame.append(self._checksum(payload))
        self._last_tx = bytes(frame)

        last_error: ProtocolServiceError | None = None
        for _attempt in range(self._retries + 1):
            self._write(frame)
            try:
                return self._read_status_frame(self._last_tx)
            except ProtocolServiceError as exc:
                last_error = exc

        assert last_error is not None
        raise last_error

    def read_status(self) -> DeviceStatus:
        raise ProtocolServiceError("自定义协议不支持独立读取状态；请发送运动命令以获取状态回帧")

    def _read_status_frame(self, transmitted_frame: bytes) -> DeviceStatus:
        serial_port = self._require_serial()
        deadline = monotonic() + max(float(serial_port.timeout or 0), 0.05)
        received = bytearray()

        while monotonic() < deadline:
            try:
                chunk = serial_port.read(max(1, serial_port.in_waiting))
            except Exception as exc:
                self._discard_broken_serial(serial_port)
                raise ProtocolServiceError(f"读取自定义协议状态失败，串口已断开: {exc}") from exc
            if chunk:
                self._last_rx += chunk
                received.extend(chunk)

            while True:
                head_index = received.find(FRAME_HEAD)
                if head_index < 0:
                    # Keep a trailing 0x41 because it may be the first byte of a
                    # frame head split across two USB reads.
                    if received[-1:] == FRAME_HEAD[:1]:
                        received[:] = received[-1:]
                    else:
                        received.clear()
                    break
                if head_index:
                    del received[:head_index]
                if len(received) < READ_FRAME_LENGTH:
                    break

                candidate = bytes(received[:READ_FRAME_LENGTH])
                del received[:READ_FRAME_LENGTH]

                # Some USB-RS485 adapters echo the transmitted request. It is
                # not a device status response, so keep waiting for the reply.
                if candidate == transmitted_frame:
                    continue

                body = list(candidate[2:-1])
                if self._checksum(body) != candidate[-1]:
                    continue
                if body[0] != self._clamp_u8(self._slave_id):
                    continue

                return DeviceStatus(
                    firmware_version=None,
                    fault_code=self._clamp_u8(body[1]),
                    state=self._clamp_u8(body[2]),
                    position=self._clamp_u8(body[3]),
                    speed=self._clamp_u8(body[4]),
                    force=self._clamp_u8(body[5]),
                )

        rx_text = self._last_rx.hex(" ") if self._last_rx else "<空>"
        raise ProtocolServiceError(
            "读取自定义协议状态失败: "
            f"TX={self._last_tx.hex(' ')}; RX={rx_text}"
        )

    def _write(self, frame: bytearray) -> None:
        serial_port = self._require_serial()
        try:
            serial_port.reset_input_buffer()
            self._last_rx = b""
            serial_port.write(frame)
            serial_port.flush()
        except Exception as exc:
            self._discard_broken_serial(serial_port)
            raise ProtocolServiceError(f"写入自定义协议运动帧失败，串口已断开: {exc}") from exc

    def _discard_broken_serial(self, serial_port: Serial) -> None:
        if self._serial is serial_port:
            self._serial = None
        try:
            serial_port.close()
        except Exception:
            pass

    def _require_serial(self) -> Serial:
        if self._serial is None or not self._serial.is_open:
            raise ProtocolServiceError("设备尚未连接")
        return self._serial

    @staticmethod
    def _checksum(values: list[int]) -> int:
        return (~sum(values)) & 0xFF

    @staticmethod
    def _clamp_u8(value: int) -> int:
        return max(0, min(255, int(value)))
